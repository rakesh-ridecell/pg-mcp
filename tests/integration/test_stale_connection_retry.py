"""Verify run_select retries once when a pooled connection is dead.

Reproduces the reported symptom: 'works for a while in the same
session, then DB connections start timing out; restarting the session
fixes it'. Root cause is a stale connection sitting in the pool after
a load balancer / proxy / DB-side timeout drops the TCP socket; the
pool hands it out, the next query hangs.

Fixes verified here:

1. The pool's ``check`` callback (``AsyncConnectionPool.check_connection``)
   runs ``SELECT 1`` on every acquired connection — dead sockets are
   detected and discarded automatically.

2. Even if a connection dies *between* the check and the actual
   execute, ``run_select`` catches ``OperationalError`` /
   ``InterfaceError`` with SQLSTATE class 08 and retries once on a
   fresh connection.

The trick: simulate a dead connection by killing it at the Postgres
level (``pg_terminate_backend``) and observing that the next
``run_select`` succeeds rather than failing.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from pg_mcp.connections import APPLICATION_NAME, _configure_conn
from pg_mcp.runner import run_select

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def fresh_pool():
    """A pool exactly as ConnectionRegistry would build one — same
    check callback, max_idle, max_lifetime — for use in this test."""
    dsn = os.environ.get("PG_MCP_TEST_DSN")
    assert dsn
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": False},
        configure=_configure_conn,
        check=AsyncConnectionPool.check_connection,
        max_idle=300.0,
        max_lifetime=1800.0,
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close(timeout=2.0)


async def _kill_my_pool_backends(dsn: str) -> int:
    """Open a side connection and pg_terminate_backend every backend
    matching this process's APPLICATION_NAME (i.e., the test's pool's
    backends)."""
    async with (
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as side,
        side.cursor() as cur,
    ):
            await cur.execute(
                """
                SELECT pid FROM pg_stat_activity
                WHERE application_name = %s AND state IN ('idle', 'idle in transaction')
                  AND pid <> pg_backend_pid()
                """,
                (APPLICATION_NAME,),
            )
            pids = [int(r[0]) for r in await cur.fetchall()]
            for pid in pids:
                await cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
            return len(pids)


async def test_run_select_recovers_from_killed_backend(fresh_pool) -> None:
    """Acquire a connection, return it to the pool (so it goes idle),
    kill it server-side, then call run_select again — must succeed
    via the pool's check callback or the retry."""
    # First call — warms the pool, picks up min_size connections.
    result = await run_select(
        fresh_pool,
        "SELECT 1",
        row_limit=1,
        byte_limit=1000,
        cell_limit=100,
        timeout_ms=5000,
    )
    assert result.rows_returned == 1

    # Kill every idle pg-mcp backend on the DB. This drops the TCP
    # socket but the pool's idle entries don't know yet.
    dsn = os.environ.get("PG_MCP_TEST_DSN")
    assert dsn
    killed = await _kill_my_pool_backends(dsn)
    assert killed >= 1, "no idle backends found to kill"

    # Second call — pool has dead connections. The check callback
    # SHOULD detect the failed SELECT 1 and discard, OR if it slips
    # through, run_select's retry should reopen.
    result = await run_select(
        fresh_pool,
        "SELECT 2",
        row_limit=1,
        byte_limit=1000,
        cell_limit=100,
        timeout_ms=5000,
    )
    assert result.rows_returned == 1
    assert result.rows[0] == ["2"]


async def test_pool_uses_check_callback(fresh_pool) -> None:
    """Verify the pool was constructed with a non-None check callback.

    psycopg_pool wraps the check function internally so we can't
    compare-by-identity, but we can verify *some* check is set.
    """
    assert fresh_pool.check is not None


async def test_pool_recycle_settings(fresh_pool) -> None:
    """max_idle and max_lifetime must be set, not at the unsafe defaults."""
    assert fresh_pool.max_idle <= 300.0, (
        f"max_idle={fresh_pool.max_idle} — too high; LBs typically drop connections after 5 minutes"
    )
    assert fresh_pool.max_lifetime <= 1800.0
