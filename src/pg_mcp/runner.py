"""Query execution: wrap a user SELECT in a READ ONLY txn and stream results.

The runner is invoked only after :func:`pg_mcp.safety.assert_readonly`
has approved the SQL. It owns:

- Transaction setup (``SET LOCAL`` timeouts, ``SET TRANSACTION READ ONLY``)
- Server-side cursor iteration with row + byte caps
- Column metadata capture for the renderer
- Notice capture
- SQLSTATE-aware exception translation
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg import sql as pg_sql
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from pg_mcp.errors import PoolExhaustedError, PostgresError, QueryTimeoutError
from pg_mcp.render import ColumnSpec, render_cell

logger = logging.getLogger(__name__)

QUERY_CANCELED = "57014"
READ_ONLY_SQL_TRANSACTION = "25006"


@dataclass
class QueryResult:
    columns: list[ColumnSpec]
    rows: list[list[str]]
    rows_returned: int
    truncated_rows: bool
    truncated_bytes: bool
    duration_ms: int
    notices: list[str] = field(default_factory=list)
    sqlstate: str | None = None  # populated only on error paths


async def run_select(
    pool: AsyncConnectionPool,
    sql: str,
    *,
    row_limit: int,
    byte_limit: int,
    cell_limit: int,
    timeout_ms: int,
    search_path: list[str] | None = None,
    acquire_timeout_s: float = 5.0,
) -> QueryResult:
    """Execute *sql* inside a READ ONLY txn and return rendered results.

    Retries once on stale-connection failures. The pool's ``check``
    callback catches most dead sockets on acquire, but a connection
    can also die between check-time and execute-time (LB drops it,
    DB restarts, network blips). When that happens psycopg raises
    one of the connection-loss exception classes, we discard the
    bad connection (the pool's broken-conn machinery handles this
    automatically), and try once more on a fresh connection.
    """
    last_exc: Exception | None = None
    for attempt in range(2):  # initial + one retry
        try:
            return await _execute_once(
                pool,
                sql,
                row_limit=row_limit,
                byte_limit=byte_limit,
                cell_limit=cell_limit,
                timeout_ms=timeout_ms,
                search_path=search_path,
                acquire_timeout_s=acquire_timeout_s,
            )
        except (
            psycopg.OperationalError,
            psycopg.InterfaceError,
        ) as e:
            # These two classes cover dropped connections, network
            # errors, server restarts, and SSL renegotiation failures.
            # Match further on SQLSTATE class 08 to avoid retrying on
            # OperationalError variants that aren't connection-loss
            # (e.g., 53300 too_many_connections).
            sqlstate = getattr(e, "sqlstate", None) or ""
            if sqlstate and not sqlstate.startswith("08"):
                # Non-connection error — surface it normally.
                raise PostgresError(str(e), sqlstate=sqlstate) from e
            last_exc = e
            if attempt == 0:
                continue
            raise PostgresError(
                f"Connection failed twice (likely stale pool). Last error: {e}",
                sqlstate=sqlstate or None,
            ) from e
    # Unreachable but keeps mypy happy.
    raise PostgresError(str(last_exc or "unknown"))


async def _execute_once(
    pool: AsyncConnectionPool,
    sql: str,
    *,
    row_limit: int,
    byte_limit: int,
    cell_limit: int,
    timeout_ms: int,
    search_path: list[str] | None,
    acquire_timeout_s: float,
) -> QueryResult:
    """One attempt at executing the query. Used by run_select; may be retried."""
    notices: list[str] = []
    started = time.perf_counter()

    try:
        async with pool.connection(timeout=acquire_timeout_s) as conn:
            conn.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
            try:
                async with conn.transaction():
                    # SET is a utility statement and does not accept bind
                    # parameters in PostgreSQL. The timeout is a bounded
                    # integer from validated config (never user input),
                    # so int()-interpolating it inline is safe.
                    await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    await conn.execute("SET LOCAL idle_in_transaction_session_timeout = 5000")
                    await conn.execute("SET TRANSACTION READ ONLY")

                    if search_path:
                        # Quote each schema identifier to prevent injection
                        # and compose the SET LOCAL search_path statement safely.
                        parts = pg_sql.SQL(", ").join(pg_sql.Identifier(s) for s in search_path)
                        stmt = pg_sql.SQL("SET LOCAL search_path = {}").format(parts)
                        await conn.execute(stmt)

                    return await _fetch(
                        conn,
                        sql,
                        row_limit=row_limit,
                        byte_limit=byte_limit,
                        cell_limit=cell_limit,
                        notices=notices,
                        started=started,
                    )
            except psycopg.errors.QueryCanceled as e:
                # Build an actionable error message. The LLM sees this
                # and should be able to retry with a narrower query.
                elapsed_s = time.perf_counter() - started
                hint = (
                    f"Query exceeded statement_timeout of {timeout_ms}ms "
                    f"(ran for {elapsed_s:.1f}s before cancellation). "
                    "Tips: "
                    "(a) For COUNT(*)/MIN/MAX on large tables, "
                    "use `table_stats` — it returns approximate row count "
                    "without a scan. "
                    "(b) For slow selects, run `explain_query` first to see the plan "
                    "and check for missing indexes. "
                    "(c) Add a narrower WHERE clause or a LIMIT. "
                    "(d) If the query must run, ask the operator to raise "
                    "`statement_timeout_ms` in the connection config."
                )
                raise QueryTimeoutError(hint) from e
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # Bubble up so run_select can decide whether to retry.
                raise
            except psycopg.Error as e:
                sqlstate = getattr(e, "sqlstate", None)
                raise PostgresError(str(e), sqlstate=sqlstate) from e
    except PoolTimeout as e:
        raise PoolExhaustedError(str(e)) from e


async def _fetch(
    conn: psycopg.AsyncConnection,
    sql: str,
    *,
    row_limit: int,
    byte_limit: int,
    cell_limit: int,
    notices: list[str],
    started: float,
) -> QueryResult:
    """Stream results via a server-side cursor, honoring row/byte caps."""
    cursor_name = f"pgmcp_{uuid.uuid4().hex}"
    rows_out: list[list[str]] = []
    bytes_used = 0
    truncated_rows = False
    truncated_bytes = False

    # Estimate a rough overhead for markdown formatting per row: 4 bytes of
    # framing + 3 bytes per cell separator.
    def _row_overhead(n_cols: int) -> int:
        return 4 + 3 * n_cols

    columns: list[ColumnSpec] = []

    async with conn.cursor(name=cursor_name) as cur:
        cur.itersize = 200
        await cur.execute(sql)
        description = cur.description or ()
        columns = [
            ColumnSpec(
                name=c.name,
                type_display=_safe_type_display(c),
            )
            for c in description
        ]

        async for raw_row in cur:
            rendered = [render_cell(v, cell_limit=cell_limit) for v in raw_row]
            row_bytes = sum(len(c) for c in rendered) + _row_overhead(len(columns))
            if bytes_used + row_bytes > byte_limit:
                truncated_bytes = True
                break
            rows_out.append(rendered)
            bytes_used += row_bytes
            if len(rows_out) >= row_limit:
                # Peek one more to set truncated_rows accurately.
                try:
                    peek = await cur.fetchone()
                    if peek is not None:
                        truncated_rows = True
                except Exception:  # pragma: no cover - defensive
                    pass
                break

    duration_ms = int((time.perf_counter() - started) * 1000)
    return QueryResult(
        columns=columns,
        rows=rows_out,
        rows_returned=len(rows_out),
        truncated_rows=truncated_rows,
        truncated_bytes=truncated_bytes,
        duration_ms=duration_ms,
        notices=list(notices),
    )


def _safe_type_display(col: psycopg.Column) -> str | None:
    """Best-effort extraction of the human-readable column type."""
    for attr in ("type_display", "display_name"):
        value = getattr(col, attr, None)
        if value:
            return str(value)
    return None


__all__ = [
    "QUERY_CANCELED",
    "READ_ONLY_SQL_TRANSACTION",
    "QueryResult",
    "run_select",
]
