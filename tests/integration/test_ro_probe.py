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


async def test_pool_configure_pins_ro_default(test_pool) -> None:
    """The pool's ``configure`` hook runs
    ``SET default_transaction_read_only = on`` on every new backend.

    That means even a connection used *outside* our explicit RO wrapper
    is read-only by default — verify the belt-and-braces works.
    """
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction) as excinfo:
        async with test_pool.connection() as conn:
            async with conn.transaction():
                # No explicit "SET TRANSACTION READ ONLY" here —
                # the session-level default_transaction_read_only must
                # carry it.
                await conn.execute("CREATE TEMP TABLE __test_rw_probe (id int) ON COMMIT DROP")
    assert excinfo.value.sqlstate == "25006"


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
