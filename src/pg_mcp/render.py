"""Render query results as markdown tables (or vertical form for wide rows).

The renderer owns the type-specific formatting rules documented in the
README. It is the single source of truth for what ends up in the LLM's
context window for every tool.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# After this many columns we switch from a horizontal markdown table to a
# vertical key/value block, which reads better in a chat UI.
WIDE_ROW_THRESHOLD = 20


@dataclass
class ColumnSpec:
    name: str
    type_display: str | None = None  # e.g., "integer", "text", "jsonb"

    def header(self) -> str:
        if self.type_display:
            return f"{_escape_md(self.name)} ({self.type_display})"
        return _escape_md(self.name)


def _escape_md(s: str) -> str:
    """Escape a string for safe inclusion in a markdown table cell."""
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br/>").replace("\r", "")


def render_cell(value: Any, *, cell_limit: int) -> str:
    """Render a single cell value for markdown output.

    Handles the full Postgres type menagerie (via psycopg's default
    adapters) and applies byte-level truncation with a visible marker.
    """
    rendered = _render_value(value)
    if len(rendered) > cell_limit:
        truncated = rendered[:cell_limit].rstrip() + "…(truncated, "
        truncated += f"original={len(rendered)} chars)"
        return truncated
    return rendered


def _render_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        hex_repr = raw.hex()
        # Cap the hex string aggressively; very long blobs are never useful.
        if len(hex_repr) > 256:
            return f"\\x{hex_repr[:256]}…(truncated, {len(raw)} bytes)"
        return f"\\x{hex_repr}"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, list):
        # Postgres arrays — render as Postgres literal form {a,b,c}
        return "{" + ",".join(_render_value(v) for v in value) + "}"
    if isinstance(value, tuple):
        # Composite / record types
        return "(" + ",".join(_render_value(v) for v in value) + ")"
    if isinstance(value, dict):
        # jsonb / hstore comes back as a dict. Compact JSON serialization.
        return json.dumps(value, default=_json_default, separators=(",", ":"))
    if isinstance(value, range):  # just in case someone maps to ranges
        return f"[{value.start},{value.stop})"
    # Fallback: str()
    s = str(value)
    # Replace NUL bytes for markdown safety
    return s.replace("\x00", "\\u0000")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return "\\x" + obj.hex()
    raise TypeError(f"cannot serialize {type(obj).__name__}")


@dataclass
class RenderResult:
    markdown: str
    byte_estimate: int


def render_table(
    columns: list[ColumnSpec],
    rows: list[list[str]],
    *,
    byte_limit: int,
) -> RenderResult:
    """Render *rows* as a markdown table (or vertical form for wide rows).

    *rows* must already be strings — callers should invoke
    :func:`render_cell` on raw values first so cell-level truncation is
    applied.
    """
    if not columns:
        return RenderResult(markdown="(no columns)", byte_estimate=13)

    if len(columns) > WIDE_ROW_THRESHOLD:
        return _render_vertical(columns, rows, byte_limit=byte_limit)

    return _render_horizontal(columns, rows, byte_limit=byte_limit)


def _render_horizontal(
    columns: list[ColumnSpec],
    rows: list[list[str]],
    *,
    byte_limit: int,
) -> RenderResult:
    header = "| " + " | ".join(c.header() for c in columns) + " |"
    separator = "|" + "|".join(["---"] * len(columns)) + "|"
    lines: list[str] = [header, separator]
    total = len(header) + len(separator) + 2
    for row in rows:
        line = "| " + " | ".join(_escape_md(cell) for cell in row) + " |"
        if total + len(line) + 1 > byte_limit:
            # Don't add this row; caller will mark as byte-truncated.
            break
        lines.append(line)
        total += len(line) + 1
    md = "\n".join(lines)
    return RenderResult(markdown=md, byte_estimate=len(md))


def _render_vertical(
    columns: list[ColumnSpec],
    rows: list[list[str]],
    *,
    byte_limit: int,
) -> RenderResult:
    lines: list[str] = []
    total = 0
    for i, row in enumerate(rows):
        if lines:
            sep = "---"
            lines.append(sep)
            total += len(sep) + 1
        block_lines: list[str] = [f"**row {i + 1}**"]
        for col, cell in zip(columns, row, strict=False):
            block_lines.append(f"- {col.header()}: {_escape_md(cell)}")
        block = "\n".join(block_lines)
        if total + len(block) + 1 > byte_limit:
            break
        lines.append(block)
        total += len(block) + 1
    md = "\n".join(lines)
    return RenderResult(markdown=md, byte_estimate=len(md))


def render_preamble(meta: dict[str, Any]) -> str:
    """Emit the metadata preamble as an HTML comment block."""
    lines = ["<!-- pg-mcp result"]
    for k, v in meta.items():
        if isinstance(v, (dict, list)):
            lines.append(f"{k}: {json.dumps(v, default=_json_default)}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("-->")
    return "\n".join(lines)


__all__ = [
    "WIDE_ROW_THRESHOLD",
    "ColumnSpec",
    "RenderResult",
    "render_cell",
    "render_preamble",
    "render_table",
]
