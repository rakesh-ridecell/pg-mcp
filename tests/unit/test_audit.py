"""Tests for audit logging — esp. the SQL-preview redaction contract."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from pg_mcp.audit import AuditLogger, _sql_preview, _sql_redact

# ---------------------------------------------------------------------------
# _sql_redact / _sql_preview
# ---------------------------------------------------------------------------


def test_redact_replaces_string_literals() -> None:
    out = _sql_redact("SELECT * FROM users WHERE email = 'alice@x.com'")
    assert "alice@x.com" not in out
    assert "?" in out


def test_redact_replaces_numeric_literals() -> None:
    out = _sql_redact("SELECT * FROM t WHERE id = 12345")
    assert "12345" not in out
    assert "?" in out


def test_redact_handles_escaped_single_quote() -> None:
    out = _sql_redact("SELECT 'O''Brien'")
    assert "O''Brien" not in out
    assert "?" in out


def test_redact_keeps_identifiers_and_keywords() -> None:
    out = _sql_redact("SELECT id FROM users WHERE email = 'x'")
    assert "SELECT" in out
    assert "users" in out
    assert "email" in out


# ---------------------------------------------------------------------------
# _sql_preview
# ---------------------------------------------------------------------------


def test_preview_redacts_literals_by_default() -> None:
    preview = _sql_preview("SELECT * FROM users WHERE email = 'alice@example.com'", redact=True)
    assert "alice@example.com" not in preview


def test_preview_preserves_literals_when_not_redacting() -> None:
    preview = _sql_preview("SELECT * FROM users WHERE email = 'alice@example.com'", redact=False)
    assert "alice@example.com" in preview


def test_preview_compacts_whitespace() -> None:
    preview = _sql_preview("SELECT\n*\nFROM\tusers", redact=False)
    assert preview == "SELECT * FROM users"


def test_preview_truncates_with_ellipsis() -> None:
    long = "SELECT " + ("x," * 200) + "1"
    preview = _sql_preview(long, redact=False, max_chars=50)
    assert len(preview) <= 50
    assert preview.endswith("…")


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def _reset_singleton() -> None:
    """AuditLogger caches a module-level logger; tests must reset between runs."""
    import logging

    logger = logging.getLogger("pg_mcp.audit")
    for h in list(logger.handlers):
        logger.removeHandler(h)


def test_log_file_is_chmod_0600(log_path: Path) -> None:
    _reset_singleton()
    AuditLogger(log_file=log_path, log_sql="hash")
    mode = stat.S_IMODE(os.stat(log_path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_tool_call_hash_mode_redacts_preview(log_path: Path) -> None:
    _reset_singleton()
    audit = AuditLogger(log_file=log_path, log_sql="hash")
    audit.tool_call(
        request_id="r1",
        tool="run_query",
        connection="prod",
        params={"limit": 1000},
        sql="SELECT * FROM users WHERE email = 'leaked@example.com'",
        duration_ms=10,
        rows_returned=1,
    )
    line = log_path.read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert "leaked@example.com" not in entry["sql_preview"]
    assert "sql_hash" in entry
    assert "sql_full" not in entry
    assert "sql_redacted" not in entry


def test_tool_call_full_mode_keeps_literals(log_path: Path) -> None:
    _reset_singleton()
    audit = AuditLogger(log_file=log_path, log_sql="full")
    audit.tool_call(
        request_id="r2",
        tool="run_query",
        connection="prod",
        params={},
        sql="SELECT * FROM users WHERE email = 'visible@example.com'",
    )
    line = log_path.read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["sql_full"] == "SELECT * FROM users WHERE email = 'visible@example.com'"
    # Preview in full mode may also contain the literal
    assert "visible@example.com" in entry["sql_preview"]


def test_tool_call_redacted_mode_includes_redacted_sql(log_path: Path) -> None:
    _reset_singleton()
    audit = AuditLogger(log_file=log_path, log_sql="redacted")
    audit.tool_call(
        request_id="r3",
        tool="run_query",
        connection="prod",
        params={},
        sql="SELECT * FROM users WHERE id = 42",
    )
    line = log_path.read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert "sql_redacted" in entry
    assert "42" not in entry["sql_redacted"]
    assert "sql_full" not in entry
