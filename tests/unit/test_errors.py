"""Error taxonomy sanity checks."""

from __future__ import annotations

import pytest

from pg_mcp.errors import (
    ConfigError,
    ConnectionUnavailableError,
    ConnectionUnsafeError,
    PgMcpError,
    PolicyViolation,
    PoolExhaustedError,
    PostgresError,
    QueryTimeoutError,
    ToolInputError,
    UnknownConnectionError,
)


def test_base_class_exists() -> None:
    assert issubclass(ConfigError, PgMcpError)
    assert issubclass(PolicyViolation, PgMcpError)
    assert issubclass(PostgresError, PgMcpError)


def test_stable_codes() -> None:
    """The `code` attribute is the documented error-code surface. Changing
    any of these requires a README update."""
    assert ConfigError.code == "config_error"
    assert ToolInputError.code == "invalid_parameter"
    assert UnknownConnectionError.code == "unknown_connection"
    assert ConnectionUnavailableError.code == "connection_unavailable"
    assert ConnectionUnsafeError.code == "connection_unsafe"
    assert PoolExhaustedError.code == "connection_pool_exhausted"
    assert QueryTimeoutError.code == "query_timeout"
    assert PostgresError.code == "postgres_error"


def test_policy_violation_shape() -> None:
    pv = PolicyViolation("disallowed_statement", "InsertStmt")
    assert pv.code == "sql_rejected_by_policy"
    assert pv.reason == "disallowed_statement"
    assert pv.detail == "InsertStmt"
    assert "InsertStmt" in str(pv)


def test_postgres_error_carries_sqlstate() -> None:
    err = PostgresError("canceled", sqlstate="57014")
    assert err.sqlstate == "57014"


def test_error_str_format() -> None:
    err = ToolInputError("limit must be > 0")
    assert "invalid_parameter" in str(err)
    assert "limit must be > 0" in str(err)


def test_can_raise_and_catch_via_base() -> None:
    with pytest.raises(PgMcpError):
        raise PolicyViolation("disallowed_statement", "InsertStmt")
