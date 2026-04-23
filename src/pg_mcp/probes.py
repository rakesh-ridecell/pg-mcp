"""Startup probes: verify each connection is truly read-only.

The critical probe attempts to ``CREATE TEMP TABLE`` inside the same
kind of ``READ ONLY`` transaction we'll use for every tool call. It
MUST fail with SQLSTATE ``25006`` (``read_only_sql_transaction``) for
the connection to be trusted. If the statement succeeds, the
connection is misconfigured (RW role, or our transaction wrapper is
broken) and must be refused.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg_pool import AsyncConnectionPool

from pg_mcp.errors import ConnectionUnsafeError

logger = logging.getLogger(__name__)


READ_ONLY_SQL_TRANSACTION = "25006"


async def probe_readonly(pool: AsyncConnectionPool) -> None:
    """Raise :class:`ConnectionUnsafeError` if the pool is not RO-safe.

    We do NOT use the shared runner here — probes are deliberately the
    simplest possible test so we're verifying the Postgres behavior,
    not any of our own code paths.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = 5000")
        await conn.execute("SET TRANSACTION READ ONLY")
        try:
            await conn.execute("CREATE TEMP TABLE __pgmcp_probe (id int) ON COMMIT DROP")
        except psycopg.Error as e:
            sqlstate = getattr(e, "sqlstate", None) or getattr(
                e.diag if hasattr(e, "diag") else None, "sqlstate", None
            )
            if sqlstate == READ_ONLY_SQL_TRANSACTION:
                logger.info("RO probe passed (SQLSTATE 25006 as expected)")
                return
            raise ConnectionUnsafeError(
                f"RO probe failed with unexpected SQLSTATE {sqlstate}: {e}"
            ) from e
        # If we get here, CREATE TEMP TABLE succeeded — the connection
        # is NOT read-only.
        raise ConnectionUnsafeError(
            "RO probe FAILED: CREATE TEMP TABLE succeeded inside a "
            "READ ONLY transaction. This connection is misconfigured "
            "or the Postgres role has write grants. Refusing to use it."
        )


__all__ = ["READ_ONLY_SQL_TRANSACTION", "probe_readonly"]
