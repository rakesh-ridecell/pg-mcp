"""Tests for the static checks in pg-mcp doctor.

Connection-level checks require a live Postgres and are exercised
indirectly via the e2e CLI tests when ``PG_MCP_TEST_DSN`` is set.
This module covers the environment checks that don't need a DB.
"""

from __future__ import annotations

import pytest

from pg_mcp.doctor import (
    _check_dependencies,
    _check_pg_mcp_binary,
    _check_python,
)


def test_python_check_is_ok() -> None:
    c = _check_python()
    assert c.severity == "ok"
    assert "Python" in c.message or "CPython" in c.message


def test_dependencies_check_all_present() -> None:
    c = _check_dependencies()
    # If dev deps aren't installed the test setup is broken.
    assert c.severity == "ok", c.message


def test_binary_check_produces_a_check() -> None:
    c = _check_pg_mcp_binary()
    # May be ok or warn depending on whether pg-mcp is on PATH; either way
    # the check has a message.
    assert c.severity in {"ok", "warn"}
    assert c.message


def test_operational_error_mapping() -> None:
    """Common Postgres errors get mapped to remediation hints."""
    from pg_mcp.config import ConnectionConfig
    from pg_mcp.doctor import _map_operational_error

    cfg = ConnectionConfig(name="x", dsn="postgresql://u@h/d")
    err = Exception('FATAL: role "pgmcp_ro" does not exist')
    c = _map_operational_error(cfg, err)
    assert c.severity == "error"
    assert c.remedy and "grants" in c.remedy.lower()


def test_operational_error_unknown_error_is_still_reported() -> None:
    from pg_mcp.config import ConnectionConfig
    from pg_mcp.doctor import _map_operational_error

    cfg = ConnectionConfig(name="x", dsn="postgresql://u@h/d")
    err = Exception("unknown problem")
    c = _map_operational_error(cfg, err)
    assert c.severity == "error"
    # No specific remedy for unknown errors — that's expected.


def test_operational_error_password_failure_hint() -> None:
    from pg_mcp.config import ConnectionConfig
    from pg_mcp.doctor import _map_operational_error

    cfg = ConnectionConfig(name="x", dsn="postgresql://u@h/d")
    err = Exception("password authentication failed for user 'u'")
    c = _map_operational_error(cfg, err)
    assert c.severity == "error"
    assert c.remedy and "password" in c.remedy.lower()


def test_operational_error_connection_refused_hint() -> None:
    from pg_mcp.config import ConnectionConfig
    from pg_mcp.doctor import _map_operational_error

    cfg = ConnectionConfig(name="x", dsn="postgresql://u@h/d")
    err = Exception("Connection refused")
    c = _map_operational_error(cfg, err)
    assert c.severity == "error"
    assert c.remedy and "psql" in c.remedy.lower()


def test_operational_error_dns_failure_hint() -> None:
    from pg_mcp.config import ConnectionConfig
    from pg_mcp.doctor import _map_operational_error

    cfg = ConnectionConfig(name="x", dsn="postgresql://u@h/d")
    err = Exception("could not translate host name")
    c = _map_operational_error(cfg, err)
    assert c.severity == "error"
    assert c.remedy and "dns" in c.remedy.lower()


@pytest.mark.parametrize(
    "msg,expected_in_remedy",
    [
        ("no pg_hba.conf entry", "pg_hba"),
        ("SSL connection error", "sslmode"),
        ("timeout expired", "timed out"),
    ],
)
def test_operational_error_various(msg: str, expected_in_remedy: str) -> None:
    from pg_mcp.config import ConnectionConfig
    from pg_mcp.doctor import _map_operational_error

    cfg = ConnectionConfig(name="x", dsn="postgresql://u@h/d")
    c = _map_operational_error(cfg, Exception(msg))
    assert c.severity == "error"
    assert c.remedy and expected_in_remedy.lower() in c.remedy.lower()
