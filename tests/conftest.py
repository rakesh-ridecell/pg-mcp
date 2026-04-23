"""Shared fixtures for unit + integration tests.

Integration tests require a live Postgres reachable via
``PG_MCP_TEST_DSN``. When that env var is absent, integration tests are
skipped automatically via pytest's ``skip_marker`` machinery.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip integration tests when there's no PG_MCP_TEST_DSN."""
    if os.environ.get("PG_MCP_TEST_DSN"):
        return
    skip_marker = pytest.mark.skip(
        reason="set PG_MCP_TEST_DSN=postgresql://... to run integration tests"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


@pytest_asyncio.fixture()
async def test_pool() -> AsyncIterator:
    """Return an AsyncConnectionPool against the test Postgres."""
    dsn = os.environ.get("PG_MCP_TEST_DSN")
    if not dsn:
        pytest.skip("PG_MCP_TEST_DSN not set")

    from psycopg_pool import AsyncConnectionPool

    from pg_mcp.connections import _configure_conn

    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=3,
        kwargs={"autocommit": False},
        configure=_configure_conn,
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()
