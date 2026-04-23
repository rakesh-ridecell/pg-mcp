"""FastMCP server: registers all 11 read-only tools for Claude Code."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from pg_mcp import __version__
from pg_mcp.audit import AuditLogger
from pg_mcp.config import Config
from pg_mcp.connections import ConnectionRegistry
from pg_mcp.errors import (
    PgMcpError,
    PolicyViolation,
    PostgresError,
    QueryTimeoutError,
    ToolInputError,
)
from pg_mcp.introspect import (
    describe_table,
    list_relations,
)
from pg_mcp.introspect import (
    list_schemas as _list_schemas,
)
from pg_mcp.introspect import (
    sample_rows as _sample_rows,
)
from pg_mcp.introspect import (
    search_schema as _search_schema,
)
from pg_mcp.introspect import (
    table_stats as _table_stats,
)
from pg_mcp.render import ColumnSpec, render_preamble, render_table
from pg_mcp.runner import run_select
from pg_mcp.safety import assert_readonly

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    registry: ConnectionRegistry
    config: Config
    audit: AuditLogger


def build_server(config: Config, audit: AuditLogger) -> FastMCP:
    """Construct a FastMCP server with all tools registered."""
    registry = ConnectionRegistry(config.connections)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        audit.startup(f"pg-mcp {__version__} starting", connections=registry.names)
        # Kick off pool opening in the background so the MCP handshake
        # isn't blocked on slow / unreachable databases. Each connection
        # surfaces its current status via `list_connections`.
        open_tasks = registry.open_all_background()
        audit.startup("pg-mcp ready (pools opening in background)")
        try:
            yield AppContext(registry=registry, config=config, audit=audit)
        finally:
            statuses = {e.config.name: e.status.value for e in registry.entries()}
            audit.shutdown("pg-mcp shutting down", connection_status=statuses)
            # Let any in-flight opens finish (best-effort) before closing.
            for task in open_tasks:
                if not task.done():
                    task.cancel()
            await registry.close_all()

    mcp = FastMCP(
        "pg-mcp",
        instructions=(
            "Read-only PostgreSQL introspection and query tools. "
            "All queries run inside a READ ONLY transaction against a "
            "SELECT-only Postgres role. Writes are blocked at three "
            "independent layers (role grants, transaction mode, SQL "
            "parser allow-list). Row cap and statement timeout enforced."
        ),
        lifespan=lifespan,
    )

    _register_tools(mcp, config, registry, audit)
    return mcp


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def _register_tools(
    mcp: FastMCP,
    config: Config,
    registry: ConnectionRegistry,
    audit: AuditLogger,
) -> None:
    defaults = config.defaults
    ro_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="list_connections",
        description=(
            "List all configured databases with their current status "
            "(available / unavailable / unsafe). Always the first tool "
            "to call when starting a new session."
        ),
        annotations=ro_annotations,
    )
    async def list_connections() -> str:
        rid = _rid()
        audit.tool_call(request_id=rid, tool="list_connections", connection=None, params={})
        lines = ["| name | status | description | last_error |", "|---|---|---|---|"]
        for entry in registry.entries():
            lines.append(
                "| {name} | {status} | {desc} | {err} |".format(
                    name=entry.config.name,
                    status=entry.status.value,
                    desc=(entry.config.description or "").replace("|", "\\|"),
                    err=(entry.last_error or "").replace("|", "\\|"),
                )
            )
        return "\n".join(lines)

    # -----------------------------------------------------------------
    @mcp.tool(
        name="list_schemas",
        description=(
            "List schemas visible to the read-only role. "
            "`pg_catalog`, `information_schema`, and `pg_toast*` / "
            "`pg_temp_*` are hidden by default — pass "
            "`include_system=true` to include them."
        ),
        annotations=ro_annotations,
    )
    async def list_schemas(connection: str, include_system: bool = False) -> str:
        rid = _rid()
        try:
            pool = registry.get_pool(connection)
            rows = await _list_schemas(pool, include_system=include_system)
        except PgMcpError as e:
            return _error_response(audit, rid, "list_schemas", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="list_schemas",
            connection=connection,
            params={"include_system": include_system},
            rows_returned=len(rows),
        )
        cols = [
            ColumnSpec("schema", "text"),
            ColumnSpec("owner", "text"),
            ColumnSpec("comment", "text"),
        ]
        data = [[r["name"], r["owner"] or "", r["comment"] or ""] for r in rows]
        result = render_table(cols, data, byte_limit=defaults.byte_limit)
        return _wrap(
            {
                "connection": connection,
                "rows_returned": len(rows),
                "tool": "list_schemas",
            },
            result.markdown,
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="list_tables",
        description=(
            "List tables (relkind r, p, f) in a schema. Partition "
            "children are hidden by default. Paginated via "
            "`limit`/`offset`."
        ),
        annotations=ro_annotations,
    )
    async def list_tables(
        connection: str,
        schema: str,
        include_partitions: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> str:
        rid = _rid()
        _require_positive(limit, "limit", max_value=2000)
        _require_non_negative(offset, "offset")
        try:
            pool = registry.get_pool(connection)
            rows, total = await list_relations(
                pool,
                schema=schema,
                relkinds=("r", "p", "f"),
                include_partition_children=include_partitions,
                limit=limit,
                offset=offset,
            )
        except PgMcpError as e:
            return _error_response(audit, rid, "list_tables", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="list_tables",
            connection=connection,
            params={
                "schema": schema,
                "include_partitions": include_partitions,
                "limit": limit,
                "offset": offset,
            },
            rows_returned=len(rows),
        )
        cols = [
            ColumnSpec("name", "text"),
            ColumnSpec("kind", "char"),
            ColumnSpec("approx_rows", "bigint"),
            ColumnSpec("total_bytes", "bigint"),
            ColumnSpec("rls", "bool"),
            ColumnSpec("partition_count", "int"),
            ColumnSpec("comment", "text"),
        ]
        data = [
            [
                r["name"],
                r["kind"],
                str(r["approximate_rows"]) if r["approximate_rows"] is not None else "NULL",
                str(r["total_bytes"]) if r["total_bytes"] is not None else "NULL",
                "true" if r["rls_enabled"] else "false",
                str(r["partition_count"]),
                r["comment"] or "",
            ]
            for r in rows
        ]
        result = render_table(cols, data, byte_limit=defaults.byte_limit)
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "rows_returned": len(rows),
                "total_count": total,
                "has_more": (offset + len(rows)) < total,
                "tool": "list_tables",
            },
            result.markdown,
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="list_views",
        description="List views and materialized views in a schema.",
        annotations=ro_annotations,
    )
    async def list_views(
        connection: str,
        schema: str,
        limit: int = 500,
        offset: int = 0,
    ) -> str:
        rid = _rid()
        _require_positive(limit, "limit", max_value=2000)
        _require_non_negative(offset, "offset")
        try:
            pool = registry.get_pool(connection)
            rows, total = await list_relations(
                pool,
                schema=schema,
                relkinds=("v", "m"),
                limit=limit,
                offset=offset,
            )
        except PgMcpError as e:
            return _error_response(audit, rid, "list_views", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="list_views",
            connection=connection,
            params={"schema": schema, "limit": limit, "offset": offset},
            rows_returned=len(rows),
        )
        cols = [
            ColumnSpec("name", "text"),
            ColumnSpec("kind", "char"),
            ColumnSpec("owner", "text"),
            ColumnSpec("comment", "text"),
        ]
        data = [[r["name"], r["kind"], r["owner"] or "", r["comment"] or ""] for r in rows]
        result = render_table(cols, data, byte_limit=defaults.byte_limit)
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "rows_returned": len(rows),
                "total_count": total,
                "has_more": (offset + len(rows)) < total,
                "tool": "list_views",
            },
            result.markdown,
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="describe_table",
        description=(
            "Full description of a table: columns (name, type, "
            "nullable, default, identity, generated, comment), primary "
            "key, unique/check constraints, foreign keys, indexes, "
            "inheritance, partitioning, RLS flag, approximate row count "
            "and size."
        ),
        annotations=ro_annotations,
    )
    async def describe_table_tool(connection: str, schema: str, table: str) -> str:
        rid = _rid()
        try:
            pool = registry.get_pool(connection)
            desc = await describe_table(pool, schema=schema, table=table)
        except PgMcpError as e:
            return _error_response(audit, rid, "describe_table", connection, e)

        if desc is None:
            audit.tool_call(
                request_id=rid,
                tool="describe_table",
                connection=connection,
                params={"schema": schema, "table": table},
                status="not_found",
            )
            return _wrap(
                {
                    "connection": connection,
                    "tool": "describe_table",
                    "error": "object_not_found_or_not_visible",
                },
                f"{schema}.{table} not found or not visible to this role.",
            )

        audit.tool_call(
            request_id=rid,
            tool="describe_table",
            connection=connection,
            params={"schema": schema, "table": table},
            rows_returned=len(desc.columns),
        )

        # Render a structured multi-section markdown block.
        out = [_format_describe(desc)]
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "table": table,
                "relkind": desc.relkind,
                "rls_enabled": desc.rls_enabled,
                "approximate_rows": desc.approximate_rows,
                "total_bytes": desc.total_bytes,
                "tool": "describe_table",
            },
            "\n\n".join(out),
        )

    # describe_view reuses describe_table (which already handles relkind v/m).
    @mcp.tool(
        name="describe_view",
        description=(
            "Describe a view or materialized view: columns and the view "
            "definition. For materialized views, also notes freshness. "
            "Flags broken views (dependencies dropped)."
        ),
        annotations=ro_annotations,
    )
    async def describe_view_tool(connection: str, schema: str, view: str) -> str:
        rid = _rid()
        try:
            pool = registry.get_pool(connection)
            desc = await describe_table(pool, schema=schema, table=view)
        except PgMcpError as e:
            return _error_response(audit, rid, "describe_view", connection, e)

        if desc is None or desc.relkind not in ("v", "m"):
            audit.tool_call(
                request_id=rid,
                tool="describe_view",
                connection=connection,
                params={"schema": schema, "view": view},
                status="not_found",
            )
            return _wrap(
                {
                    "connection": connection,
                    "tool": "describe_view",
                    "error": "object_not_found_or_not_visible",
                },
                f"{schema}.{view} is not a view/matview or not visible.",
            )

        audit.tool_call(
            request_id=rid,
            tool="describe_view",
            connection=connection,
            params={"schema": schema, "view": view},
            status="ok" if desc.view_status == "ok" else desc.view_status,
        )
        body = _format_describe(desc)
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "view": view,
                "relkind": desc.relkind,
                "view_status": desc.view_status,
                "view_error": desc.view_error,
                "tool": "describe_view",
            },
            body,
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="sample_rows",
        description=(
            "Return up to `limit` rows from a table or view. "
            "Includes `table_estimated_rows` and `rls_enabled` in the "
            "preamble so you can distinguish an empty table from an "
            "RLS-filtered read."
        ),
        annotations=ro_annotations,
    )
    async def sample_rows_tool(
        connection: str,
        schema: str,
        table: str,
        limit: int = 20,
    ) -> str:
        rid = _rid()
        _require_positive(limit, "limit", max_value=1000)
        try:
            pool = registry.get_pool(connection)
            conn_cfg = config.get(connection)
            timeout = (
                conn_cfg.statement_timeout_ms
                if conn_cfg and conn_cfg.statement_timeout_ms
                else defaults.statement_timeout_ms
            )
            result, approx, rls = await _sample_rows(
                pool,
                schema=schema,
                table=table,
                limit=limit,
                row_limit=limit,
                byte_limit=defaults.byte_limit,
                cell_limit=defaults.cell_limit,
                timeout_ms=timeout,
            )
        except PgMcpError as e:
            return _error_response(audit, rid, "sample_rows", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="sample_rows",
            connection=connection,
            params={"schema": schema, "table": table, "limit": limit},
            rows_returned=result.rows_returned,
            duration_ms=result.duration_ms,
        )
        rendered = render_table(result.columns, result.rows, byte_limit=defaults.byte_limit)
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "table": table,
                "rows_returned": result.rows_returned,
                "duration_ms": result.duration_ms,
                "truncated_rows": result.truncated_rows,
                "truncated_bytes": result.truncated_bytes,
                "table_estimated_rows": approx,
                "rls_enabled": rls,
                "tool": "sample_rows",
            },
            rendered.markdown or "(no rows)",
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="run_query",
        description=(
            "Execute a read-only SELECT (or EXPLAIN). Guaranteed to "
            "never mutate: blocked at three layers (role grants, "
            "READ ONLY transaction, parser allow-list + function "
            "deny-list). Multi-statement, CTE-DML, EXPLAIN ANALYZE-DML, "
            "and dangerous functions (pg_read_file, pg_advisory_lock, "
            "nextval, dblink_exec, ...) are rejected. Results capped by "
            "`limit` and the server's byte cap."
        ),
        annotations=ro_annotations,
    )
    async def run_query(
        connection: str,
        sql: str,
        limit: int | None = None,
    ) -> str:
        rid = _rid()
        conn_cfg = config.get(connection)
        row_limit = (
            limit
            if limit is not None
            else (conn_cfg.row_limit if conn_cfg and conn_cfg.row_limit else defaults.row_limit)
        )
        _require_positive(row_limit, "limit", max_value=defaults.row_limit)
        timeout = (
            conn_cfg.statement_timeout_ms
            if conn_cfg and conn_cfg.statement_timeout_ms
            else defaults.statement_timeout_ms
        )

        # Parser check BEFORE touching Postgres.
        try:
            assert_readonly(sql)
        except PolicyViolation as e:
            return _error_response(audit, rid, "run_query", connection, e, sql=sql)

        try:
            pool = registry.get_pool(connection)
            result = await run_select(
                pool,
                sql,
                row_limit=row_limit,
                byte_limit=defaults.byte_limit,
                cell_limit=defaults.cell_limit,
                timeout_ms=timeout,
                search_path=conn_cfg.search_path if conn_cfg else None,
                acquire_timeout_s=defaults.acquire_timeout_s,
            )
        except PgMcpError as e:
            return _error_response(audit, rid, "run_query", connection, e, sql=sql)

        audit.tool_call(
            request_id=rid,
            tool="run_query",
            connection=connection,
            params={"limit": row_limit},
            sql=sql,
            duration_ms=result.duration_ms,
            rows_returned=result.rows_returned,
            truncated_rows=result.truncated_rows,
            truncated_bytes=result.truncated_bytes,
        )
        rendered = render_table(result.columns, result.rows, byte_limit=defaults.byte_limit)
        return _wrap(
            {
                "connection": connection,
                "duration_ms": result.duration_ms,
                "rows_returned": result.rows_returned,
                "truncated_rows": result.truncated_rows,
                "truncated_bytes": result.truncated_bytes,
                "notices": result.notices,
                "tool": "run_query",
            },
            rendered.markdown + f"\n\n({result.rows_returned} rows)",
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="explain_query",
        description=(
            "Return EXPLAIN output for a SELECT. Pass `analyze=true` to "
            "run EXPLAIN ANALYZE (only permitted for plain SELECTs; "
            "EXPLAIN ANALYZE of DML is rejected because it would "
            "actually execute the DML)."
        ),
        annotations=ro_annotations,
    )
    async def explain_query(
        connection: str,
        sql: str,
        analyze: bool = False,
    ) -> str:
        rid = _rid()
        explain_sql = f"EXPLAIN ({'ANALYZE, ' if analyze else ''}VERBOSE, COSTS) {sql}"
        try:
            assert_readonly(explain_sql)
        except PolicyViolation as e:
            return _error_response(audit, rid, "explain_query", connection, e, sql=sql)

        conn_cfg = config.get(connection)
        timeout = (
            conn_cfg.statement_timeout_ms
            if conn_cfg and conn_cfg.statement_timeout_ms
            else defaults.statement_timeout_ms
        )
        try:
            pool = registry.get_pool(connection)
            result = await run_select(
                pool,
                explain_sql,
                row_limit=10_000,
                byte_limit=defaults.byte_limit,
                cell_limit=defaults.cell_limit,
                timeout_ms=timeout,
                search_path=conn_cfg.search_path if conn_cfg else None,
            )
        except PgMcpError as e:
            return _error_response(audit, rid, "explain_query", connection, e, sql=sql)

        audit.tool_call(
            request_id=rid,
            tool="explain_query",
            connection=connection,
            params={"analyze": analyze},
            sql=sql,
            duration_ms=result.duration_ms,
            rows_returned=result.rows_returned,
        )
        plan_lines = [r[0] for r in result.rows] if result.rows else []
        body = "```\n" + "\n".join(plan_lines) + "\n```"
        return _wrap(
            {
                "connection": connection,
                "duration_ms": result.duration_ms,
                "analyze": analyze,
                "tool": "explain_query",
            },
            body,
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="search_schema",
        description=(
            "Fuzzy-search tables, views, columns, and functions by "
            "name/comment substring. `kind` can be 'all', 'table', "
            "'view', 'column', or 'function'."
        ),
        annotations=ro_annotations,
    )
    async def search_schema_tool(
        connection: str,
        pattern: str,
        kind: str = "all",
        limit: int = 100,
    ) -> str:
        rid = _rid()
        if kind not in ("all", "table", "view", "column", "function"):
            return _error_response(
                audit,
                rid,
                "search_schema",
                connection,
                ToolInputError(f"kind must be one of all/table/view/column/function, got {kind!r}"),
            )
        _require_positive(limit, "limit", max_value=500)
        if not pattern or len(pattern) < 1:
            return _error_response(
                audit,
                rid,
                "search_schema",
                connection,
                ToolInputError("pattern must be non-empty"),
            )

        try:
            pool = registry.get_pool(connection)
            hits = await _search_schema(pool, pattern=pattern, kind=kind, limit=limit)
        except PgMcpError as e:
            return _error_response(audit, rid, "search_schema", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="search_schema",
            connection=connection,
            params={"pattern": pattern, "kind": kind, "limit": limit},
            rows_returned=len(hits),
        )
        cols = [
            ColumnSpec("kind", "text"),
            ColumnSpec("schema", "text"),
            ColumnSpec("name", "text"),
            ColumnSpec("parent", "text"),
            ColumnSpec("comment", "text"),
        ]
        data = [[h.kind, h.schema, h.name, h.parent or "", h.comment or ""] for h in hits]
        rendered = render_table(cols, data, byte_limit=defaults.byte_limit)
        return _wrap(
            {
                "connection": connection,
                "pattern": pattern,
                "kind": kind,
                "rows_returned": len(hits),
                "tool": "search_schema",
            },
            rendered.markdown,
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="table_stats",
        description=(
            "Return statistics for a table: approximate row count, "
            "total size, relation and index size, live/dead tuple "
            "counts, last vacuum and analyze timestamps."
        ),
        annotations=ro_annotations,
    )
    async def table_stats_tool(connection: str, schema: str, table: str) -> str:
        rid = _rid()
        try:
            pool = registry.get_pool(connection)
            stats = await _table_stats(pool, schema=schema, table=table)
        except PgMcpError as e:
            return _error_response(audit, rid, "table_stats", connection, e)

        if stats is None:
            audit.tool_call(
                request_id=rid,
                tool="table_stats",
                connection=connection,
                params={"schema": schema, "table": table},
                status="not_found",
            )
            return _wrap(
                {
                    "connection": connection,
                    "tool": "table_stats",
                    "error": "object_not_found_or_not_visible",
                },
                f"{schema}.{table} not found or not visible to this role.",
            )

        audit.tool_call(
            request_id=rid,
            tool="table_stats",
            connection=connection,
            params={"schema": schema, "table": table},
        )
        lines = [
            f"- **Table**: `{stats.schema}.{stats.name}`",
            f"- Approximate rows: `{stats.approximate_rows}`",
            f"- Total size: `{_human_bytes(stats.total_bytes)}`",
            f"- Relation size: `{_human_bytes(stats.relation_bytes)}`",
            f"- Indexes size: `{_human_bytes(stats.indexes_bytes)}`",
            f"- Live tuples: `{stats.n_live_tup}`",
            f"- Dead tuples: `{stats.n_dead_tup}`",
            f"- Last vacuum: `{stats.last_vacuum}`",
            f"- Last analyze: `{stats.last_analyze}`",
            f"- Pages on disk: `{stats.relpages}`",
        ]
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "table": table,
                "tool": "table_stats",
            },
            "\n".join(lines),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rid() -> str:
    return uuid.uuid4().hex[:12]


def _wrap(meta: dict[str, Any], body: str) -> str:
    return render_preamble(meta) + "\n\n" + body


def _error_response(
    audit: AuditLogger,
    rid: str,
    tool: str,
    connection: str | None,
    err: PgMcpError,
    *,
    sql: str | None = None,
) -> str:
    extra: dict[str, Any] = {}
    if isinstance(err, PolicyViolation):
        extra["reason"] = err.reason
    if isinstance(err, PostgresError):
        extra["sqlstate"] = err.sqlstate
    if isinstance(err, QueryTimeoutError):
        extra["timeout"] = True
    audit.tool_call(
        request_id=rid,
        tool=tool,
        connection=connection,
        params={},
        sql=sql,
        status="error",
        error_code=err.code,
        sqlstate=extra.get("sqlstate"),
    )
    preamble = {
        "connection": connection,
        "tool": tool,
        "error": err.code,
        **extra,
    }
    return _wrap(preamble, f"**error**: {err.code}: {err.detail}")


def _require_positive(value: int, name: str, *, max_value: int | None = None) -> None:
    if value <= 0:
        raise ToolInputError(f"{name} must be > 0, got {value}")
    if max_value is not None and value > max_value:
        raise ToolInputError(f"{name} must be <= {max_value}, got {value}")


def _require_non_negative(value: int, name: str) -> None:
    if value < 0:
        raise ToolInputError(f"{name} must be >= 0, got {value}")


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "NULL"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} PiB"


def _format_describe(desc: Any) -> str:
    """Render a TableDescription as a multi-section markdown document."""
    lines: list[str] = []
    lines.append(f"## `{desc.schema}.{desc.name}`")
    kind_labels = {
        "r": "table",
        "p": "partitioned table",
        "f": "foreign table",
        "v": "view",
        "m": "materialized view",
    }
    lines.append(
        f"_{kind_labels.get(desc.relkind, desc.relkind)}_"
        + (f" — owner: `{desc.owner}`" if desc.owner else "")
    )
    if desc.comment:
        lines.append(f"> {desc.comment}")
    lines.append("")
    lines.append(
        f"- Approximate rows: `{desc.approximate_rows}` — "
        f"total size `{_human_bytes(desc.total_bytes)}` — "
        f"RLS `{'enabled' if desc.rls_enabled else 'disabled'}`"
    )
    if desc.inherits_from:
        lines.append("- Inherits from: " + ", ".join(f"`{p}`" for p in desc.inherits_from))
    if desc.partition_strategy:
        lines.append(
            f"- Partitioned: `{desc.partition_strategy}` on `{desc.partition_key}` "
            f"({desc.partition_count} children)"
        )

    # Columns
    lines.append("")
    lines.append("### Columns")
    lines.append("| # | name | type | null | default | identity | generated | comment |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for col in desc.columns:
        lines.append(
            f"| {col.ordinal} | `{col.name}` | {col.type} | "
            f"{'YES' if col.nullable else 'NO'} | "
            f"{col.default or ''} | {col.identity or ''} | "
            f"{col.generated or ''} | {col.comment or ''} |"
        )

    if desc.primary_key:
        lines.append("")
        lines.append(f"### Primary key\n`{', '.join('`' + c + '`' for c in desc.primary_key)}`")

    if desc.unique_constraints:
        lines.append("")
        lines.append("### Unique constraints")
        for c in desc.unique_constraints:
            lines.append(f"- `{c.name}`: `{c.definition}`")

    if desc.check_constraints:
        lines.append("")
        lines.append("### Check constraints")
        for c in desc.check_constraints:
            lines.append(f"- `{c.name}`: `{c.definition}`")

    if desc.foreign_keys:
        lines.append("")
        lines.append("### Foreign keys")
        for fk in desc.foreign_keys:
            lines.append(f"- `{fk.name}`: `{fk.references_table}`")

    if desc.indexes:
        lines.append("")
        lines.append("### Indexes")
        for ix in desc.indexes:
            badges = []
            if ix.is_primary:
                badges.append("PK")
            if ix.is_unique:
                badges.append("UNIQUE")
            badge = f" ({', '.join(badges)})" if badges else ""
            lines.append(f"- `{ix.name}`{badge}: `{ix.definition}`")

    if desc.view_definition:
        lines.append("")
        lines.append("### View definition")
        if desc.view_status != "ok":
            lines.append(f"> **Status: {desc.view_status}** — {desc.view_error}")
        lines.append("```sql")
        lines.append(desc.view_definition.rstrip())
        lines.append("```")

    return "\n".join(lines)


__all__ = ["AppContext", "build_server"]
