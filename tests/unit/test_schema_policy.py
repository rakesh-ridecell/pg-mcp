"""Tests for per-connection schema allow/deny policy."""

from __future__ import annotations

import pytest

from pg_mcp.errors import PolicyViolation
from pg_mcp.safety import SchemaPolicy, extract_referenced_schemas

# ---------------------------------------------------------------------------
# SchemaPolicy.is_allowed
# ---------------------------------------------------------------------------


def test_empty_policy_allows_everything() -> None:
    p = SchemaPolicy()
    assert p.is_allowed("public")
    assert p.is_allowed("pg_catalog")
    assert p.is_allowed("internal")


def test_deny_list_blocks() -> None:
    p = SchemaPolicy(denied=["audit", "pii"])
    assert p.is_allowed("public")
    assert not p.is_allowed("audit")
    assert not p.is_allowed("pii")


def test_allow_list_restricts() -> None:
    p = SchemaPolicy(allowed=["public", "app"])
    assert p.is_allowed("public")
    assert p.is_allowed("app")
    assert not p.is_allowed("internal")


def test_case_insensitive() -> None:
    p = SchemaPolicy(allowed=["Public"], denied=["Audit"])
    assert p.is_allowed("public")
    assert p.is_allowed("PUBLIC")
    assert not p.is_allowed("audit")


def test_deny_beats_allow() -> None:
    p = SchemaPolicy(allowed=["public", "audit"], denied=["audit"])
    assert p.is_allowed("public")
    assert not p.is_allowed("audit")


# ---------------------------------------------------------------------------
# extract_referenced_schemas
# ---------------------------------------------------------------------------


def test_extracts_qualified_schema() -> None:
    schemas = extract_referenced_schemas("SELECT * FROM app.users")
    assert schemas == {"app"}


def test_unqualified_references_are_not_collected() -> None:
    # Unqualified table refs rely on search_path — we don't know which
    # schema they'll resolve to, so they're not collected.
    schemas = extract_referenced_schemas("SELECT * FROM users")
    assert schemas == set()


def test_multiple_schemas_via_join() -> None:
    schemas = extract_referenced_schemas(
        "SELECT u.id FROM app.users u JOIN audit.events e ON u.id = e.user_id"
    )
    assert schemas == {"app", "audit"}


def test_schema_in_cte_is_collected() -> None:
    schemas = extract_referenced_schemas("WITH x AS (SELECT * FROM app.users) SELECT * FROM x")
    assert schemas == {"app"}


def test_parse_error_returns_empty() -> None:
    assert extract_referenced_schemas("SELECT 1 FROM") == set()


# ---------------------------------------------------------------------------
# SchemaPolicy.check_sql
# ---------------------------------------------------------------------------


def test_check_sql_empty_policy_permits_all() -> None:
    p = SchemaPolicy()
    p.check_sql("SELECT * FROM app.users")  # must not raise


def test_check_sql_rejects_denied_schema() -> None:
    p = SchemaPolicy(denied=["audit"])
    with pytest.raises(PolicyViolation) as excinfo:
        p.check_sql("SELECT * FROM audit.events")
    assert excinfo.value.reason == "disallowed_schema"
    assert "audit" in excinfo.value.detail


def test_check_sql_rejects_outside_allow_list() -> None:
    p = SchemaPolicy(allowed=["public"])
    with pytest.raises(PolicyViolation) as excinfo:
        p.check_sql("SELECT * FROM app.users")
    assert excinfo.value.reason == "disallowed_schema"


def test_check_sql_permits_unqualified_refs() -> None:
    # Unqualified refs aren't blocked by the schema policy — the
    # Postgres role's USAGE grants are the gate for those. This is
    # documented behavior.
    p = SchemaPolicy(allowed=["public"])
    p.check_sql("SELECT * FROM users")  # must not raise


def test_check_sql_rejects_any_disallowed_schema_in_multi_table_query() -> None:
    p = SchemaPolicy(denied=["audit"])
    with pytest.raises(PolicyViolation):
        p.check_sql("SELECT u.id FROM app.users u JOIN audit.events e ON e.user_id = u.id")
