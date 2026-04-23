"""Integration tests for the runner against a real Postgres.

Skipped automatically if ``PG_MCP_TEST_DSN`` is not set.

The test DSN should point at a database where the *user has SELECT on
at least one table*; no writes are ever performed. A bare Postgres with
`postgres` superuser works fine since the tests only SELECT.
"""

from __future__ import annotations

import pytest

from pg_mcp.errors import PolicyViolation, QueryTimeoutError
from pg_mcp.runner import run_select
from pg_mcp.safety import assert_readonly

pytestmark = [pytest.mark.integration]


async def test_basic_select(test_pool) -> None:
    result = await run_select(
        test_pool,
        "SELECT 1 AS one, 'hello' AS greeting",
        row_limit=10,
        byte_limit=10_000,
        cell_limit=1000,
        timeout_ms=5000,
    )
    assert result.rows_returned == 1
    assert result.rows[0] == ["1", "hello"]
    assert [c.name for c in result.columns] == ["one", "greeting"]


async def test_generate_series_limit(test_pool) -> None:
    result = await run_select(
        test_pool,
        "SELECT generate_series(1, 10000) AS n",
        row_limit=100,
        byte_limit=10_000_000,
        cell_limit=1000,
        timeout_ms=5000,
    )
    assert result.rows_returned == 100
    assert result.truncated_rows is True


async def test_statement_timeout(test_pool) -> None:
    with pytest.raises(QueryTimeoutError):
        await run_select(
            test_pool,
            "SELECT pg_sleep(10)",
            row_limit=10,
            byte_limit=1000,
            cell_limit=100,
            timeout_ms=500,
        )


async def test_parser_blocks_before_pg(test_pool) -> None:
    """Prove that the parser rejects dangerous SQL without reaching PG."""
    with pytest.raises(PolicyViolation):
        assert_readonly("DROP TABLE whatever")


async def test_ro_txn_blocks_writes_even_if_parser_bypassed(test_pool) -> None:
    """Prove layer 2 (``SET TRANSACTION READ ONLY``) rejects writes
    independently of the parser.

    We can't pass DDL through ``run_select`` because it uses a
    server-side cursor and ``DECLARE … CURSOR FOR <ddl>`` is itself a
    syntax error. Instead we recreate the runner's exact transaction
    envelope against the pool and attempt a write directly — this
    isolates the RO layer from the cursor + parser layers.
    """
    import psycopg

    with pytest.raises(psycopg.Error) as excinfo:
        async with test_pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = 5000")
                await conn.execute("SET TRANSACTION READ ONLY")
                # Attempt the same write the parser rejects. With the
                # RO wrapper in place, PG must respond with 25006.
                await conn.execute("CREATE TEMP TABLE __test_pgmcp_layer2 (id int)")
    assert excinfo.value.sqlstate == "25006", (
        f"expected read_only_sql_transaction (25006), got {excinfo.value.sqlstate}: {excinfo.value}"
    )


async def test_notices_captured(test_pool) -> None:
    result = await run_select(
        test_pool,
        "SELECT 1",
        row_limit=1,
        byte_limit=1000,
        cell_limit=100,
        timeout_ms=5000,
    )
    # No notices expected for a trivial SELECT, but the field should exist.
    assert result.notices == []


async def test_wide_result_byte_cap(test_pool) -> None:
    """A query producing large per-row output should be truncated by
    the byte cap, not allowed to blow memory."""
    result = await run_select(
        test_pool,
        "SELECT repeat('x', 10000) AS big FROM generate_series(1, 1000)",
        row_limit=1000,
        byte_limit=50_000,  # ~5 rows of 10k each before cap kicks in
        cell_limit=20_000,
        timeout_ms=5000,
    )
    assert result.truncated_bytes is True
    assert result.rows_returned < 100
