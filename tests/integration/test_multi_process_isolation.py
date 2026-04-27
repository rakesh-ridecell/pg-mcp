"""Cross-process isolation: two parallel pg-mcp processes don't interfere.

Reproduces the bug reported in production:

    'I cannot connect to same DB from 2 different opencode sessions
    parallely'

Root cause: every pg-mcp process used to tag its connections with
``application_name = 'pg-mcp'``. The ``cancel_query`` /
``reconnect`` helpers found and cancelled every ``'pg-mcp'``
backend, including ones owned by *other* pg-mcp processes. So if
session A reconnected, session B's running queries got cancelled
out of nowhere.

Fix: each process tags its connections with
``application_name = 'pg-mcp/<our_pid>'`` and the cancel helper
matches that exact value.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from pg_mcp.config import ConnectionConfig
from pg_mcp.connections import APPLICATION_NAME, ConnectionEntry, _cancel_backends, _configure_conn

pytestmark = [pytest.mark.integration]


async def test_application_name_includes_pid() -> None:
    """Verify the per-process application_name tag contains our PID."""
    assert f"pg-mcp/{os.getpid()}" == APPLICATION_NAME
    assert "/" in APPLICATION_NAME


async def test_two_pools_in_one_process_dont_share_app_name(test_pool) -> None:
    """Within one process, every backend uses the same APPLICATION_NAME.

    (This is fine — they share the process, so cancelling them is
    intended.)
    """
    async with test_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SHOW application_name")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == APPLICATION_NAME


def _child_process_target(dsn: str, child_app_name_q: mp.Queue, hold_query_for_s: float) -> None:
    """Worker that opens its own pool and runs a long-ish query."""

    async def _run() -> None:
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=2,
            kwargs={"autocommit": False},
            configure=_configure_conn,
            open=False,
        )
        await pool.open(wait=True, timeout=10)
        try:
            # Tell the parent what app_name this child is using.
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SHOW application_name")
                row = await cur.fetchone()
                child_app_name_q.put(row[0] if row else "")

            # Now run a long sleep so the parent has time to attempt
            # cancellation. If cancellation succeeds we'll see
            # QueryCanceled (SQLSTATE 57014) — we treat that as a test
            # FAILURE (the parent's cancel reached us, which means
            # cross-process isolation is broken). If we sleep through
            # cleanly, isolation works.
            try:
                async with pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(f"SELECT pg_sleep({hold_query_for_s})")
                child_app_name_q.put("query_completed")
            except psycopg.errors.QueryCanceled:
                child_app_name_q.put("query_cancelled")
        finally:
            await pool.close(timeout=2.0)

    asyncio.run(_run())


async def test_one_process_cannot_cancel_another_process_queries(test_pool) -> None:
    """Spawn a child process, have it run a 5s query, and try to cancel
    it from the parent. The cancel must NOT reach the child's query."""
    import os

    dsn = os.environ.get("PG_MCP_TEST_DSN")
    assert dsn

    ctx = (
        mp.get_context("fork")
        if mp.get_start_method(allow_none=False) != "spawn"
        else mp.get_context("spawn")
    )
    child_q: mp.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_child_process_target,
        args=(dsn, child_q, 4.0),  # child holds query for 4s
    )
    proc.start()
    try:
        # Wait for child to report its app_name
        child_app_name = child_q.get(timeout=10)
        assert child_app_name.startswith("pg-mcp/")
        assert child_app_name != APPLICATION_NAME, (
            "child process must use a different application_name"
        )

        # Give the child time to actually issue the SELECT pg_sleep.
        await asyncio.sleep(0.5)

        # Now from the parent (this process), try to cancel "our"
        # backends. If our match is too broad, this would reach the
        # child's pg_sleep too.
        cfg = ConnectionConfig(name="t", dsn=dsn)
        entry = ConnectionEntry(config=cfg)
        n = await _cancel_backends(entry)
        # n could be 0 (no parent-process queries running) or any number
        # of *parent's own* queries. The critical assertion is what
        # the child observes:
        result = child_q.get(timeout=10)
        assert result == "query_completed", (
            f"child reported {result!r} — cancellation crossed processes! "
            f"(parent cancelled {n} backends)"
        )
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)
