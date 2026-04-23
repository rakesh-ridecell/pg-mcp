"""FastMCP server: registers all 11 read-only tools for Claude Code."""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from pg_mcp import __version__
from pg_mcp.audit import AuditLogger
from pg_mcp.config import Config
from pg_mcp.connections import ConnectionRegistry, ConnectionStatus
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
    diff_schemas as _diff_schemas,
)
from pg_mcp.introspect import (
    list_schemas as _list_schemas,
)
from pg_mcp.introspect import (
    related_tables as _related_tables,
)
from pg_mcp.introspect import (
    sample_rows as _sample_rows,
)
from pg_mcp.introspect import (
    sample_rows_with_where as _sample_rows_with_where,
)
from pg_mcp.introspect import (
    search_schema as _search_schema,
)
from pg_mcp.introspect import (
    slow_queries as _slow_queries,
)
from pg_mcp.introspect import (
    table_stats as _table_stats,
)
from pg_mcp.ratelimit import RateLimiter
from pg_mcp.render import ColumnSpec, render_preamble, render_table
from pg_mcp.runner import run_select
from pg_mcp.safety import SchemaPolicy, assert_readonly

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

    # Build a SchemaPolicy + RateLimiter per connection upfront.
    schema_policies: dict[str, SchemaPolicy] = {
        c.name: SchemaPolicy(allowed=c.allowed_schemas, denied=c.denied_schemas)
        for c in config.connections
    }
    rate_limiters: dict[str, RateLimiter] = {
        c.name: RateLimiter(limit_per_minute=c.rate_limit_per_minute) for c in config.connections
    }

    def _schema_policy(name: str) -> SchemaPolicy:
        return schema_policies.get(name, SchemaPolicy())

    async def _rate_limit(name: str) -> None:
        """Raise RateLimitedError if the connection is over its budget."""
        limiter = rate_limiters.get(name)
        if limiter is not None and limiter.enabled:
            await limiter.check_and_record(name)

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
        lines = [
            "| name | status | pool (open/max, waiting) | description | last_error |",
            "|---|---|---|---|---|",
        ]
        for entry in registry.entries():
            pool_cell = "-"
            if entry.pool is not None:
                try:
                    stats = entry.pool.get_stats()
                    open_n = stats.get("pool_size", 0)
                    max_n = stats.get("pool_max", entry.config.pool.max_size)
                    waiting = stats.get("requests_waiting", 0)
                    pool_cell = f"{open_n}/{max_n}, {waiting}"
                except Exception:
                    pool_cell = "?"
            lines.append(
                "| {name} | {status} | {pool} | {desc} | {err} |".format(
                    name=entry.config.name,
                    status=entry.status.value,
                    pool=pool_cell,
                    desc=(entry.config.description or "").replace("|", "\\|"),
                    err=(entry.last_error or "").replace("|", "\\|"),
                )
            )
        return "\n".join(lines)

    # -----------------------------------------------------------------
    @mcp.tool(
        name="reconnect",
        description=(
            "Close and re-open the pool for a named connection, re-running "
            "the read-only probe. Use this when `list_connections` shows a "
            "connection as `unavailable` (network flapped, DB restarted, "
            "credentials rotated) — it avoids restarting the MCP server."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,  # changes server-internal state
            destructiveHint=False,  # doesn't touch the DB
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def reconnect(connection: str) -> str:
        rid = _rid()
        try:
            entry = await registry.reopen(connection)
        except PgMcpError as e:
            return _error_response(audit, rid, "reconnect", connection, e)
        audit.tool_call(
            request_id=rid,
            tool="reconnect",
            connection=connection,
            params={},
            status="ok" if entry.status == ConnectionStatus.AVAILABLE else "degraded",
        )
        return _wrap(
            {
                "connection": connection,
                "new_status": entry.status.value,
                "last_error": entry.last_error,
                "tool": "reconnect",
            },
            f"Connection `{connection}` is now **{entry.status.value}**"
            + (f" — {entry.last_error}" if entry.last_error else ""),
        )

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
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
            rows = await _list_schemas(pool, include_system=include_system)
        except PgMcpError as e:
            return _error_response(audit, rid, "list_schemas", connection, e)

        # Apply the config-level schema allow/deny filter — hide schemas
        # the LLM shouldn't see in this connection.
        policy = _schema_policy(connection)
        rows = [r for r in rows if policy.is_allowed(r["name"])]

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
        try:
            _require_identifier(schema, "schema")
        except ToolInputError as e:
            return _error_response(audit, rid, "list_tables", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "list_tables",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        _require_positive(limit, "limit", max_value=2000)
        _require_non_negative(offset, "offset")
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
        try:
            _require_identifier(schema, "schema")
        except ToolInputError as e:
            return _error_response(audit, rid, "list_views", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "list_views",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        _require_positive(limit, "limit", max_value=2000)
        _require_non_negative(offset, "offset")
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
            _require_identifier(schema, "schema")
            _require_identifier(table, "table")
        except ToolInputError as e:
            return _error_response(audit, rid, "describe_table", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "describe_table",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
            _require_identifier(schema, "schema")
            _require_identifier(view, "view")
        except ToolInputError as e:
            return _error_response(audit, rid, "describe_view", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "describe_view",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
            "Return up to `limit` rows from a table or view. Optional "
            "`where` parameter accepts a SQL WHERE clause (without the "
            "'WHERE' keyword) to filter rows — fully validated through "
            "the safety gate, so DML smuggling and deny-listed function "
            "calls in the WHERE are rejected. Preamble includes "
            "`table_estimated_rows` and `rls_enabled` so you can "
            "distinguish empty from RLS-filtered."
        ),
        annotations=ro_annotations,
    )
    async def sample_rows_tool(
        connection: str,
        schema: str,
        table: str,
        limit: int = 20,
        where: str | None = None,
    ) -> str:
        rid = _rid()
        try:
            _require_identifier(schema, "schema")
            _require_identifier(table, "table")
        except ToolInputError as e:
            return _error_response(audit, rid, "sample_rows", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "sample_rows",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        _require_positive(limit, "limit", max_value=1000)
        conn_cfg = config.get(connection)
        timeout = (
            conn_cfg.statement_timeout_ms
            if conn_cfg and conn_cfg.statement_timeout_ms
            else defaults.statement_timeout_ms
        )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
            if where:
                result, approx, rls = await _sample_rows_with_where(
                    pool,
                    schema=schema,
                    table=table,
                    where=where,
                    limit=limit,
                    row_limit=limit,
                    byte_limit=defaults.byte_limit,
                    cell_limit=defaults.cell_limit,
                    timeout_ms=timeout,
                )
            else:
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
        except PolicyViolation as e:
            return _error_response(audit, rid, "sample_rows", connection, e)
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
            _schema_policy(connection).check_sql(sql)
        except PolicyViolation as e:
            return _error_response(audit, rid, "run_query", connection, e, sql=sql)

        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
        # Strip a leading EXPLAIN (...) from the user's SQL so we don't
        # wrap EXPLAIN-in-EXPLAIN, which is a parse error.
        inner_sql = _strip_leading_explain(sql)
        # Validate the inner SQL first — this gives a cleaner error
        # ("disallowed_statement: InsertStmt") rather than the
        # double-wrapped version that might confuse the LLM.
        try:
            assert_readonly(inner_sql)
            _schema_policy(connection).check_sql(inner_sql)
        except PolicyViolation as e:
            return _error_response(audit, rid, "explain_query", connection, e, sql=sql)
        explain_sql = f"EXPLAIN ({'ANALYZE, ' if analyze else ''}VERBOSE, COSTS) {inner_sql}"
        # Re-check the wrapped form in case ANALYZE-of-DML somehow slipped
        # through (e.g., inner was a CTE-DML we missed — shouldn't happen,
        # but belt-and-braces).
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
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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
            _require_identifier(schema, "schema")
            _require_identifier(table, "table")
        except ToolInputError as e:
            return _error_response(audit, rid, "table_stats", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "table_stats",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
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

    # -----------------------------------------------------------------
    @mcp.tool(
        name="related_tables",
        description=(
            "Walk the foreign-key graph for a table and return every "
            "FK relationship touching it — both outgoing (this table "
            "→ parent) and incoming (child → this table). The best "
            "first call when figuring out what to JOIN to."
        ),
        annotations=ro_annotations,
    )
    async def related_tables_tool(connection: str, schema: str, table: str) -> str:
        rid = _rid()
        try:
            _require_identifier(schema, "schema")
            _require_identifier(table, "table")
        except ToolInputError as e:
            return _error_response(audit, rid, "related_tables", connection, e)
        if not _schema_policy(connection).is_allowed(schema):
            return _error_response(
                audit,
                rid,
                "related_tables",
                connection,
                PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted"),
            )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
            rels = await _related_tables(pool, schema=schema, table=table)
        except PgMcpError as e:
            return _error_response(audit, rid, "related_tables", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="related_tables",
            connection=connection,
            params={"schema": schema, "table": table},
            rows_returned=len(rels),
        )
        if not rels:
            return _wrap(
                {
                    "connection": connection,
                    "schema": schema,
                    "table": table,
                    "tool": "related_tables",
                    "rows_returned": 0,
                },
                f"No foreign-key relationships found for `{schema}.{table}`.",
            )
        lines = [
            "| direction | local cols | → | other table | other cols | on update | on delete |",
            "|---|---|---|---|---|---|---|",
        ]
        for rel in rels:
            arrow = "→" if rel.direction == "outgoing" else "←"
            lines.append(
                "| {d} | ({lc}) | {a} | `{ot}` | ({oc}) | {ou} | {od} |".format(
                    d=rel.direction,
                    lc=", ".join(rel.local_columns),
                    a=arrow,
                    ot=f"{rel.other_schema}.{rel.other_table}",
                    oc=", ".join(rel.other_columns),
                    ou=rel.on_update or "NO ACTION",
                    od=rel.on_delete or "NO ACTION",
                )
            )
        return _wrap(
            {
                "connection": connection,
                "schema": schema,
                "table": table,
                "rows_returned": len(rels),
                "tool": "related_tables",
            },
            "\n".join(lines),
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="slow_queries",
        description=(
            "Top slow queries from `pg_stat_statements` (must be "
            "installed on the DB). Useful for performance work. "
            "Returns query text, call count, mean/total/max execution "
            "time in ms, and rows affected."
        ),
        annotations=ro_annotations,
    )
    async def slow_queries_tool(
        connection: str,
        limit: int = 20,
        min_mean_ms: float = 0.0,
    ) -> str:
        rid = _rid()
        _require_positive(limit, "limit", max_value=200)
        if min_mean_ms < 0:
            return _error_response(
                audit,
                rid,
                "slow_queries",
                connection,
                ToolInputError("min_mean_ms must be >= 0"),
            )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
            results = await _slow_queries(pool, limit=limit, min_mean_ms=min_mean_ms)
        except PgMcpError as e:
            return _error_response(audit, rid, "slow_queries", connection, e)

        if results is None:
            audit.tool_call(
                request_id=rid,
                tool="slow_queries",
                connection=connection,
                params={"limit": limit, "min_mean_ms": min_mean_ms},
                status="error",
                error_code="extension_missing",
            )
            return _wrap(
                {
                    "connection": connection,
                    "tool": "slow_queries",
                    "error": "extension_missing",
                },
                "`pg_stat_statements` is not installed on this database. "
                "Ask a DBA to `CREATE EXTENSION pg_stat_statements;` "
                "(requires superuser + shared_preload_libraries).",
            )

        audit.tool_call(
            request_id=rid,
            tool="slow_queries",
            connection=connection,
            params={"limit": limit, "min_mean_ms": min_mean_ms},
            rows_returned=len(results),
        )
        lines = [
            "| calls | mean ms | total ms | max ms | rows | query |",
            "|---|---|---|---|---|---|",
        ]
        for q in results:
            # Preview the query — compact whitespace + truncate.
            query_text = " ".join(q.query_text.split())
            if len(query_text) > 140:
                query_text = query_text[:139] + "…"
            query_text = query_text.replace("|", "\\|")
            lines.append(
                f"| {q.calls} | {q.mean_exec_time_ms:.1f} | "
                f"{q.total_exec_time_ms:.1f} | {q.max_exec_time_ms:.1f} | "
                f"{q.rows} | `{query_text}` |"
            )
        return _wrap(
            {
                "connection": connection,
                "rows_returned": len(results),
                "tool": "slow_queries",
            },
            "\n".join(lines),
        )

    # -----------------------------------------------------------------
    @mcp.tool(
        name="diff_schemas",
        description=(
            "Compare two schemas in the same database: which tables "
            "exist in only one, and for common tables, which columns "
            "differ in type / nullable / default. Great for "
            "prod-vs-staging migration work."
        ),
        annotations=ro_annotations,
    )
    async def diff_schemas_tool(
        connection: str,
        schema_a: str,
        schema_b: str,
    ) -> str:
        rid = _rid()
        try:
            _require_identifier(schema_a, "schema_a")
            _require_identifier(schema_b, "schema_b")
        except ToolInputError as e:
            return _error_response(audit, rid, "diff_schemas", connection, e)
        policy = _schema_policy(connection)
        for s in (schema_a, schema_b):
            if not policy.is_allowed(s):
                return _error_response(
                    audit,
                    rid,
                    "diff_schemas",
                    connection,
                    PolicyViolation("disallowed_schema", f"schema {s!r} is not permitted"),
                )
        try:
            await _rate_limit(connection)
            pool = await registry.await_pool(connection)
            diff = await _diff_schemas(pool, schema_a=schema_a, schema_b=schema_b)
        except PgMcpError as e:
            return _error_response(audit, rid, "diff_schemas", connection, e)

        audit.tool_call(
            request_id=rid,
            tool="diff_schemas",
            connection=connection,
            params={"schema_a": schema_a, "schema_b": schema_b},
        )
        sections = [f"## Diff: `{schema_a}` ↔ `{schema_b}`"]
        if diff.only_in_a:
            sections.append(f"### Only in `{schema_a}`")
            sections.extend(f"- `{t}`" for t in diff.only_in_a)
        if diff.only_in_b:
            sections.append(f"### Only in `{schema_b}`")
            sections.extend(f"- `{t}`" for t in diff.only_in_b)
        if diff.common_tables_with_differences:
            sections.append("### Common tables with differences")
            for entry in diff.common_tables_with_differences:
                sections.append(f"#### `{entry['table']}`")
                if entry.get("columns_only_in_a"):
                    sections.append(
                        f"- columns only in `{schema_a}`: "
                        + ", ".join(f"`{c}`" for c in entry["columns_only_in_a"])
                    )
                if entry.get("columns_only_in_b"):
                    sections.append(
                        f"- columns only in `{schema_b}`: "
                        + ", ".join(f"`{c}`" for c in entry["columns_only_in_b"])
                    )
                if entry.get("columns_changed"):
                    for ch in entry["columns_changed"]:
                        sections.append(
                            f"- `{ch['column']}`: "
                            f"{schema_a}=({ch['a']['type']}, nullable={ch['a']['nullable']}, "
                            f"default={ch['a']['default']}) "
                            f"vs {schema_b}=({ch['b']['type']}, nullable={ch['b']['nullable']}, "
                            f"default={ch['b']['default']})"
                        )
        if not diff.only_in_a and not diff.only_in_b and not diff.common_tables_with_differences:
            sections.append("**Schemas are structurally identical.**")
        return _wrap(
            {
                "connection": connection,
                "schema_a": schema_a,
                "schema_b": schema_b,
                "only_in_a": len(diff.only_in_a),
                "only_in_b": len(diff.only_in_b),
                "differences": len(diff.common_tables_with_differences),
                "tool": "diff_schemas",
            },
            "\n".join(sections),
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


# Identifier validation — defence against non-string / empty / absurd
# inputs before they reach psycopg.sql.Identifier. psycopg would handle
# quoting correctly, but the error message surfaced to the LLM is much
# clearer if we reject up front.
_IDENT_MAX_LEN = 63  # PostgreSQL's NAMEDATALEN-1


def _require_identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string, got {type(value).__name__}")
    if not value:
        raise ToolInputError(f"{name} must be non-empty")
    if len(value) > _IDENT_MAX_LEN:
        raise ToolInputError(
            f"{name} is {len(value)} chars; Postgres identifiers are max {_IDENT_MAX_LEN}"
        )
    # Control characters and NUL bytes have no business in an identifier.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ToolInputError(f"{name} contains control characters")
    return value


_LEADING_EXPLAIN_RE = re.compile(
    r"""
    \A                              # start
    \s*                             # leading whitespace
    EXPLAIN                         # keyword
    (?:\s*\([^)]*\))?               # optional parenthesized options, e.g., (ANALYZE, VERBOSE)
    (?:\s+(?:ANALYZE|VERBOSE))*     # legacy-form trailing keywords (pre-9.0 syntax)
    \s+                             # whitespace before the inner stmt
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _strip_leading_explain(sql: str) -> str:
    """Remove a leading ``EXPLAIN [(...)]`` from *sql* if present.

    Used by ``explain_query`` so users can pass either an already-
    prefixed ``EXPLAIN SELECT ...`` or a bare ``SELECT ...`` without
    producing invalid ``EXPLAIN EXPLAIN ...`` SQL. When nothing is
    stripped, returns *sql* unchanged.
    """
    match = _LEADING_EXPLAIN_RE.match(sql)
    return sql[match.end() :] if match else sql


def _human_bytes(n: int | None) -> str:
    """Render a byte count as a human-readable string, preserving one
    decimal place. Uses float division so ``1_500_000_000`` renders as
    ``1.4 GiB``, not ``1.0 GiB``.
    """
    if n is None:
        return "NULL"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


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
            cols = ", ".join(f"`{c}`" for c in fk.columns) or "?"
            ref_cols = ", ".join(f"`{c}`" for c in fk.references_columns) or "?"
            actions = []
            if fk.on_update and fk.on_update != "NO ACTION":
                actions.append(f"ON UPDATE {fk.on_update}")
            if fk.on_delete and fk.on_delete != "NO ACTION":
                actions.append(f"ON DELETE {fk.on_delete}")
            action_suffix = f" ({', '.join(actions)})" if actions else ""
            lines.append(
                f"- `{fk.name}`: ({cols}) → `{fk.references_table}` ({ref_cols})" + action_suffix
            )

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
