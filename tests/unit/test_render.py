"""Tests for the result-rendering contract.

The output of every cell of every type is stable across releases. If a
test here changes, the README's rendering contract must change too.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pg_mcp.render import (
    WIDE_ROW_THRESHOLD,
    ColumnSpec,
    render_cell,
    render_preamble,
    render_table,
)

# ---------------------------------------------------------------------------
# Per-type cell rendering
# ---------------------------------------------------------------------------


def test_null_renders_as_uppercase_literal() -> None:
    assert render_cell(None, cell_limit=100) == "NULL"


def test_bools() -> None:
    assert render_cell(True, cell_limit=100) == "true"
    assert render_cell(False, cell_limit=100) == "false"


def test_ints_and_floats() -> None:
    assert render_cell(42, cell_limit=100) == "42"
    assert render_cell(-1, cell_limit=100) == "-1"
    assert render_cell(3.14, cell_limit=100) == "3.14"


def test_decimal_preserves_precision() -> None:
    assert render_cell(Decimal("1.2300"), cell_limit=100) == "1.2300"


def test_uuid() -> None:
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert render_cell(u, cell_limit=100) == "12345678-1234-5678-1234-567812345678"


def test_bytes_hex_short() -> None:
    out = render_cell(b"\x01\x02\x03", cell_limit=100)
    assert out == "\\x010203"


def test_bytes_hex_truncated_for_large_blobs() -> None:
    out = render_cell(b"\xff" * 200, cell_limit=10_000)
    assert out.startswith("\\xff")
    assert "truncated, 200 bytes" in out


def test_datetime_isoformat() -> None:
    d = dt.datetime(2026, 4, 23, 10, 0, 0, tzinfo=dt.UTC)
    out = render_cell(d, cell_limit=100)
    assert out == "2026-04-23T10:00:00+00:00"


def test_date_isoformat() -> None:
    assert render_cell(dt.date(2026, 4, 23), cell_limit=100) == "2026-04-23"


def test_array_uses_pg_literal_form() -> None:
    assert render_cell([1, 2, 3], cell_limit=100) == "{1,2,3}"


def test_nested_array() -> None:
    assert render_cell([[1, 2], [3, 4]], cell_limit=100) == "{{1,2},{3,4}}"


def test_composite_tuple() -> None:
    assert render_cell((1, "a", None), cell_limit=100) == "(1,a,NULL)"


def test_jsonb_compact() -> None:
    out = render_cell({"a": 1, "b": "two"}, cell_limit=100)
    # Keys may be in insertion order in CPython 3.7+; allow either
    assert out.startswith("{") and out.endswith("}")
    assert '"a":1' in out
    assert '"b":"two"' in out


def test_nul_byte_escape() -> None:
    out = render_cell("NUL\x00HERE", cell_limit=100)
    assert "\\u0000" in out
    assert "\x00" not in out


def test_cell_truncation_mid_string() -> None:
    out = render_cell("X" * 500, cell_limit=50)
    assert len(out) < 200  # truncated marker is included
    assert "truncated" in out
    assert "original=500" in out


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


def test_empty_table_is_handled() -> None:
    result = render_table([], [], byte_limit=1000)
    assert "no columns" in result.markdown


def test_markdown_header_with_type_annotations() -> None:
    cols = [ColumnSpec("id", "integer"), ColumnSpec("name", "text")]
    rows = [["1", "alice"]]
    result = render_table(cols, rows, byte_limit=1000)
    assert "id (integer)" in result.markdown
    assert "name (text)" in result.markdown
    assert "| 1 | alice |" in result.markdown


def test_pipe_in_cell_is_escaped() -> None:
    cols = [ColumnSpec("a", "text")]
    rows = [["has|pipe"]]
    result = render_table(cols, rows, byte_limit=1000)
    assert r"has\|pipe" in result.markdown


def test_newline_in_cell_is_escaped() -> None:
    cols = [ColumnSpec("a", "text")]
    rows = [["line1\nline2"]]
    result = render_table(cols, rows, byte_limit=1000)
    assert "<br/>" in result.markdown
    assert "\n" not in result.markdown.split("\n", 2)[-1].split(" ", 4)[-1]


def test_pipe_in_column_name_is_escaped() -> None:
    cols = [ColumnSpec("weird|name", "text")]
    rows = [["x"]]
    result = render_table(cols, rows, byte_limit=1000)
    assert r"weird\|name" in result.markdown


def test_wide_row_flips_to_vertical() -> None:
    cols = [ColumnSpec(f"c{i}", "integer") for i in range(WIDE_ROW_THRESHOLD + 5)]
    rows = [[str(i) for i in range(WIDE_ROW_THRESHOLD + 5)]]
    result = render_table(cols, rows, byte_limit=10_000)
    # Vertical form uses "**row 1**" header
    assert "**row 1**" in result.markdown
    assert "| c0 | c1 |" not in result.markdown


def test_byte_limit_truncates_rows() -> None:
    cols = [ColumnSpec("x", "int")]
    rows = [[str(i) * 100] for i in range(100)]
    result = render_table(cols, rows, byte_limit=500)
    # Only a few rows should fit under the budget
    assert result.byte_estimate <= 600


# ---------------------------------------------------------------------------
# Preamble
# ---------------------------------------------------------------------------


def test_preamble_is_html_comment() -> None:
    preamble = render_preamble({"connection": "prod", "rows_returned": 27})
    assert preamble.startswith("<!-- pg-mcp result")
    assert preamble.endswith("-->")
    assert "connection: prod" in preamble
    assert "rows_returned: 27" in preamble


def test_preamble_serializes_nested_data_as_json() -> None:
    preamble = render_preamble({"notices": ["warning 1", "warning 2"]})
    assert '"warning 1"' in preamble
    assert '"warning 2"' in preamble
    assert "notices:" in preamble
