"""Structured JSONL audit logging.

One JSON object per line, rotated at 50 MiB x 5 files by default.
Mirrored to stderr at WARN and above so operators see critical events
even without tailing the log file.

Three SQL-logging modes control how much of the user-submitted SQL is
written to disk:

- ``hash``     — SHA-256 hash + 200-char preview. Safe default.
- ``redacted`` — SQL with string/numeric literals replaced by ``?``.
- ``full``     — full SQL. Opt-in only; documented as a PII risk.

Multiple pg-mcp processes (e.g., one per OpenCode session) all write
to the same default log file. Single-line appends are atomic on
POSIX so entries don't interleave, but rotation isn't coordinated
across processes — under heavy load the older rotated files may lose
a few lines. Every entry includes a ``pid`` field so you can filter
by source process: ``jq 'select(.pid == 12345)' pg-mcp.log``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Literal

LogSqlMode = Literal["hash", "redacted", "full"]

# Cached at module-load time. Used to demux multi-process logs.
_PID = os.getpid()

_LITERAL_RE = re.compile(
    r"""
    '([^']|'')*'          # single-quoted strings (with escaped '')
    | \b\d+(\.\d+)?([eE][+-]?\d+)?\b  # numeric literals
    """,
    re.VERBOSE,
)


def _sql_hash(sql: str) -> str:
    return "sha256:" + hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def _sql_redact(sql: str) -> str:
    return _LITERAL_RE.sub("?", sql)


def _sql_preview(sql: str, *, redact: bool, max_chars: int = 200) -> str:
    """First N chars of a compacted SQL string.

    When *redact* is true (the default for ``hash`` and ``redacted``
    modes), string and numeric literals are replaced with ``?`` before
    truncation so the preview cannot leak literal values (e.g., emails
    in a WHERE clause).
    """
    source = _sql_redact(sql) if redact else sql
    compact = " ".join(source.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1] + "…"


def platform_default_log_path() -> Path:
    """Return the platform-appropriate default log file path."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "pg-mcp" / "pg-mcp.log"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "pg-mcp" / "pg-mcp.log"


class AuditLogger:
    """JSONL audit logger with rotation + stderr mirror at WARN+."""

    def __init__(
        self,
        *,
        log_file: Path | None = None,
        log_sql: LogSqlMode = "hash",
        max_bytes: int = 50 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self.log_sql_mode: LogSqlMode = log_sql
        self._logger = logging.getLogger("pg_mcp.audit")
        self._logger.setLevel(logging.INFO)
        # Prevent propagation to the root logger — we don't want audit
        # entries to show up twice in stderr.
        self._logger.propagate = False

        if self._logger.handlers:
            # Already configured (e.g., re-init in tests)
            return

        if log_file is None:
            log_file = platform_default_log_path()
        log_file = Path(log_file).expanduser()

        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            # Audit logs may contain SQL fragments with sensitive data
            # (even in hash mode the file exists and grows). Lock it to
            # owner-only. `touch()` first so the file exists for chmod
            # even if nothing has been written yet.
            try:
                log_file.touch(exist_ok=True)
                log_file.chmod(0o600)
            except OSError:
                # best effort on unusual filesystems (NFS, tmpfs, ...)
                pass
        except OSError as e:
            # Fall back to stderr-only if the file location is unwritable.
            sys.stderr.write(
                f"[pg-mcp] audit log file unwritable ({e}); falling back to stderr-only\n"
            )
            handler = logging.StreamHandler(stream=sys.stderr)

        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

        # Stderr mirror for WARN+
        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        stderr_handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(stderr_handler)

    # -----------------------------------------------------------------

    def _encode_sql(self, sql: str) -> dict[str, Any]:
        # The preview is always redacted unless the operator has
        # explicitly opted into `full` mode — otherwise literals in a
        # WHERE clause (emails, user IDs, secrets) would leak even in
        # hash mode.
        redact_preview = self.log_sql_mode != "full"
        out: dict[str, Any] = {
            "sql_hash": _sql_hash(sql),
            "sql_preview": _sql_preview(sql, redact=redact_preview),
        }
        if self.log_sql_mode == "redacted":
            out["sql_redacted"] = _sql_redact(sql)
        elif self.log_sql_mode == "full":
            out["sql_full"] = sql
        return out

    def tool_call(
        self,
        *,
        request_id: str,
        tool: str,
        connection: str | None,
        params: dict[str, Any],
        sql: str | None = None,
        duration_ms: int | None = None,
        rows_returned: int | None = None,
        truncated_rows: bool | None = None,
        truncated_bytes: bool | None = None,
        status: str = "ok",
        error_code: str | None = None,
        sqlstate: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": _utc_now(),
            "pid": _PID,
            "event": "tool_call",
            "request_id": request_id,
            "tool": tool,
            "connection": connection,
            "params": params,
            "duration_ms": duration_ms,
            "rows_returned": rows_returned,
            "truncated_rows": truncated_rows,
            "truncated_bytes": truncated_bytes,
            "status": status,
            "error_code": error_code,
            "sqlstate": sqlstate,
        }
        if sql is not None:
            entry.update(self._encode_sql(sql))
        self._emit(entry, level=logging.INFO if status == "ok" else logging.WARNING)

    def startup(self, message: str, **extra: Any) -> None:
        self._emit(
            {
                "ts": _utc_now(),
                "pid": _PID,
                "event": "startup",
                "message": message,
                **extra,
            },
            level=logging.INFO,
        )

    def shutdown(self, message: str, **extra: Any) -> None:
        self._emit(
            {
                "ts": _utc_now(),
                "pid": _PID,
                "event": "shutdown",
                "message": message,
                **extra,
            },
            level=logging.INFO,
        )

    def error(self, message: str, **extra: Any) -> None:
        self._emit(
            {
                "ts": _utc_now(),
                "pid": _PID,
                "event": "error",
                "message": message,
                **extra,
            },
            level=logging.ERROR,
        )

    # -----------------------------------------------------------------

    def _emit(self, entry: dict[str, Any], *, level: int) -> None:
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
        except Exception as e:  # pragma: no cover - defensive
            line = json.dumps(
                {
                    "ts": _utc_now(),
                    "pid": _PID,
                    "event": "log_encoding_failure",
                    "error": str(e),
                }
            )
        self._logger.log(level, line)


def _utc_now() -> str:
    # Millisecond-precision ISO 8601 UTC.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + (
        f".{int((time.time() % 1) * 1000):03d}Z"
    )


__all__ = ["AuditLogger", "LogSqlMode", "platform_default_log_path"]
