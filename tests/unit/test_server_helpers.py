"""Tests for server-level helpers (identifier validation, EXPLAIN stripping, byte formatting)."""

from __future__ import annotations

import pytest

from pg_mcp.errors import ToolInputError
from pg_mcp.server import (
    _human_bytes,
    _require_identifier,
    _strip_leading_explain,
)

# ---------------------------------------------------------------------------
# _human_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (None, "NULL"),
        (0, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (10_000_000, "9.5 MiB"),
        (1_500_000_000, "1.4 GiB"),
        (1024**5, "1.0 PiB"),
    ],
)
def test_human_bytes(n: int | None, expected: str) -> None:
    assert _human_bytes(n) == expected


# ---------------------------------------------------------------------------
# _require_identifier
# ---------------------------------------------------------------------------


def test_require_identifier_accepts_simple_name() -> None:
    assert _require_identifier("users", "table") == "users"


def test_require_identifier_accepts_mixed_case() -> None:
    assert _require_identifier("MyTable", "table") == "MyTable"


def test_require_identifier_rejects_non_string() -> None:
    with pytest.raises(ToolInputError) as excinfo:
        _require_identifier(123, "table")  # type: ignore[arg-type]
    assert "must be a string" in str(excinfo.value)


def test_require_identifier_rejects_list() -> None:
    with pytest.raises(ToolInputError):
        _require_identifier(["a", "b"], "schema")  # type: ignore[arg-type]


def test_require_identifier_rejects_empty() -> None:
    with pytest.raises(ToolInputError) as excinfo:
        _require_identifier("", "table")
    assert "non-empty" in str(excinfo.value)


def test_require_identifier_rejects_overlong() -> None:
    with pytest.raises(ToolInputError) as excinfo:
        _require_identifier("x" * 100, "table")
    assert "63" in str(excinfo.value)


def test_require_identifier_rejects_control_chars() -> None:
    with pytest.raises(ToolInputError):
        _require_identifier("bad\x00name", "table")
    with pytest.raises(ToolInputError):
        _require_identifier("bad\nname", "table")


# ---------------------------------------------------------------------------
# _strip_leading_explain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("EXPLAIN SELECT 1", "SELECT 1"),
        ("explain select 1", "select 1"),
        ("  EXPLAIN  SELECT 1", "SELECT 1"),
        ("EXPLAIN (VERBOSE) SELECT 1", "SELECT 1"),
        ("EXPLAIN (ANALYZE, COSTS) SELECT 1", "SELECT 1"),
        ("EXPLAIN ANALYZE SELECT 1", "SELECT 1"),
        ("EXPLAIN VERBOSE SELECT 1", "SELECT 1"),
        # Only strips a LEADING explain — inner EXPLAIN keyword in a
        # subquery should be left alone.
        (
            "SELECT 1 FROM (SELECT 2) x",
            "SELECT 1 FROM (SELECT 2) x",
        ),
    ],
)
def test_strip_leading_explain(sql: str, expected: str) -> None:
    assert _strip_leading_explain(sql) == expected
