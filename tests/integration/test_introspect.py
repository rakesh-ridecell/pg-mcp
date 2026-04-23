"""Integration tests for catalog introspection."""

from __future__ import annotations

import pytest

from pg_mcp.introspect import (
    describe_table,
    list_relations,
    list_schemas,
    sample_rows,
    search_schema,
    table_stats,
)

pytestmark = [pytest.mark.integration]


async def test_list_schemas_hides_system_by_default(test_pool) -> None:
    schemas = await list_schemas(test_pool, include_system=False)
    names = {s["name"] for s in schemas}
    assert "pg_catalog" not in names
    assert "information_schema" not in names


async def test_list_schemas_include_system(test_pool) -> None:
    schemas = await list_schemas(test_pool, include_system=True)
    names = {s["name"] for s in schemas}
    assert "pg_catalog" in names
    assert "information_schema" in names


async def test_list_tables_pg_catalog(test_pool) -> None:
    rows, total = await list_relations(
        test_pool,
        schema="pg_catalog",
        relkinds=("r",),
        limit=10,
    )
    names = {r["name"] for r in rows}
    assert "pg_class" in names or total > 0


async def test_describe_pg_class(test_pool) -> None:
    desc = await describe_table(test_pool, schema="pg_catalog", table="pg_class")
    assert desc is not None
    assert desc.relkind == "r"
    column_names = {c.name for c in desc.columns}
    # Essential columns that have existed since forever:
    assert {"oid", "relname", "relkind", "relnamespace"}.issubset(column_names)


async def test_describe_nonexistent(test_pool) -> None:
    desc = await describe_table(test_pool, schema="public", table="definitely_nope_12345")
    assert desc is None


async def test_sample_pg_class(test_pool) -> None:
    result, _approx, rls = await sample_rows(
        test_pool,
        schema="pg_catalog",
        table="pg_class",
        limit=5,
        row_limit=5,
        byte_limit=100_000,
        cell_limit=1000,
        timeout_ms=5000,
    )
    assert result.rows_returned <= 5
    assert rls is False  # pg_class has no RLS


async def test_search_schema_matches_pg_class(test_pool) -> None:
    hits = await search_schema(test_pool, pattern="pg_class", kind="table", include_system=True)
    # At minimum pg_class itself should be found
    assert any(h.name == "pg_class" for h in hits)


async def test_table_stats_pg_class(test_pool) -> None:
    stats = await table_stats(test_pool, schema="pg_catalog", table="pg_class")
    assert stats is not None
    assert stats.approximate_rows is not None
