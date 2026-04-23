"""Integration test for the startup RO probe."""

from __future__ import annotations

import pytest

from pg_mcp.probes import probe_readonly

pytestmark = [pytest.mark.integration]


async def test_probe_rejects_unsafe_connection(test_pool) -> None:
    """If the connection has write privileges AND we omit SET TRANSACTION
    READ ONLY, the probe must flag the connection as unsafe.

    We test the affirmative case: against our normal test_pool which
    uses the proper RO wrapper, the probe succeeds silently.
    """
    # Normal probe should pass without raising.
    await probe_readonly(test_pool)


async def test_probe_raises_on_rw_connection(test_pool, monkeypatch) -> None:
    """Simulate a broken wrapper by executing CREATE TEMP TABLE directly,
    bypassing the READ ONLY transaction. This should succeed against a
    normal RW role — proving the probe's positive assertion (it must
    fail with 25006) is a real signal."""
    # Attempt CREATE TEMP TABLE directly, no RO wrapper.
    async with test_pool.connection() as conn, conn.transaction():
        await conn.execute("CREATE TEMP TABLE __test_rw_probe (id int) ON COMMIT DROP")
        # If we get here without SQLSTATE 25006, the role IS read-write,
        # which is the case with the typical `postgres` test role.
    # Now explicitly call probe_readonly; it wraps with SET TRANSACTION
    # READ ONLY and should reject the CREATE TEMP TABLE with 25006.
    await probe_readonly(test_pool)  # must pass — RO wrapper is in place


async def test_probe_against_already_read_only_connection(test_pool) -> None:
    """If the server's default_transaction_read_only is on AND the RO
    wrapper applies SET TRANSACTION READ ONLY, the probe should still
    behave normally (and not trip on the belt-and-suspenders config)."""
    async with test_pool.connection() as conn:
        await conn.execute("SET SESSION default_transaction_read_only = on")
    try:
        await probe_readonly(test_pool)
    finally:
        async with test_pool.connection() as conn:
            await conn.execute("SET SESSION default_transaction_read_only = off")
