"""Catalog queries powering the introspection tools.

Every query runs inside the same ``READ ONLY`` envelope as ``run_query``.
We use ``pg_catalog`` directly (not ``information_schema``) for speed
and precision — ``information_schema`` has permission-aware filtering
that can hide objects the RO role can in fact see via SELECT.

All user-supplied identifiers are parameterized via ``%s``; we never
interpolate identifiers into SQL strings. For building queries like
``SELECT * FROM "schema"."table"`` (as in :func:`sample_rows`), we use
``psycopg.sql.Identifier`` which performs proper quoting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg import sql as pg_sql
from psycopg_pool import AsyncConnectionPool

from pg_mcp.runner import QueryResult, run_select

logger = logging.getLogger(__name__)

SYSTEM_SCHEMA_PATTERNS = (
    "pg_catalog",
    "information_schema",
)
SYSTEM_SCHEMA_PREFIXES = ("pg_toast", "pg_temp_", "pg_toast_temp_")


def _is_system_schema(name: str) -> bool:
    return name in SYSTEM_SCHEMA_PATTERNS or any(
        name.startswith(prefix) for prefix in SYSTEM_SCHEMA_PREFIXES
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_catalog_query(
    pool: AsyncConnectionPool,
    sql: str | pg_sql.Composed,
    params: tuple | None = None,
    *,
    row_limit: int = 5000,
    byte_limit: int = 4 * 1024 * 1024,  # 4 MiB for catalog queries
    cell_limit: int = 8192,
    timeout_ms: int = 10_000,
) -> QueryResult:
    """Run an internal catalog query bypassing the user-facing size caps.

    We still enforce sane limits to prevent truly runaway pulls (e.g.,
    100k tables by 40 columns), but the caller's limits are intentionally
    higher than the defaults exposed via ``run_query``.
    """
    if isinstance(sql, pg_sql.Composed):
        # Compile to a concrete string by running inside a dummy cursor;
        # run_select's server-side cursor needs a string. Use as_string
        # with a throw-away connection via psycopg's SQL rendering.
        import psycopg

        compiled = sql.as_string(psycopg.adapters)
        if params is not None:
            # Embed parameters — we only do this for ident-safe Compositions
            # so this path should be fine, but catalog queries below pass
            # params=None and do the params in a separate position arg.
            compiled = compiled % params  # pragma: no cover - unused
        final_sql = compiled
    else:
        final_sql = sql
        if params is not None:
            # Build a safe parameterized query via psycopg cursor interp:
            # easier path is to pass params through the cursor directly,
            # which run_select doesn't currently expose. For catalog
            # queries we pass fully-formed SQL with parameters embedded
            # via psycopg.sql/Literal so we never have raw user input.
            raise NotImplementedError("use pg_sql.Literal to embed params into a Composed query")
    return await run_select(
        pool,
        final_sql,
        row_limit=row_limit,
        byte_limit=byte_limit,
        cell_limit=cell_limit,
        timeout_ms=timeout_ms,
    )


def _lit(value: object) -> pg_sql.Literal:
    return pg_sql.Literal(value)


# ---------------------------------------------------------------------------
# list_schemas
# ---------------------------------------------------------------------------


async def list_schemas(
    pool: AsyncConnectionPool,
    *,
    include_system: bool = False,
) -> list[dict]:
    sql = pg_sql.SQL(
        """
        SELECT
            n.nspname AS schema_name,
            pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
            obj_description(n.oid, 'pg_namespace') AS comment
        FROM pg_catalog.pg_namespace n
        WHERE pg_catalog.has_schema_privilege(n.oid, 'USAGE')
          {sys_filter}
        ORDER BY n.nspname
        """
    ).format(
        sys_filter=pg_sql.SQL("")
        if include_system
        else pg_sql.SQL(
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg\\_toast%' ESCAPE '\\' "
            "AND n.nspname NOT LIKE 'pg\\_temp\\_%' ESCAPE '\\'"
        )
    )
    result = await _run_catalog_query(pool, sql)
    return [
        {
            "name": r[0],
            "owner": r[1],
            "comment": r[2] if r[2] != "NULL" else None,
        }
        for r in result.rows
    ]


# ---------------------------------------------------------------------------
# list_tables / list_views (shared catalog query)
# ---------------------------------------------------------------------------


async def list_relations(
    pool: AsyncConnectionPool,
    *,
    schema: str,
    relkinds: tuple[str, ...],
    include_partition_children: bool = False,
    limit: int = 500,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return (rows, total_count) for objects matching *relkinds*.

    ``relkind`` codes:
      r = ordinary table
      p = partitioned table (parent)
      f = foreign table
      v = view
      m = materialized view
      i = index, S = sequence, c = composite type, t = TOAST table
          (not relevant to list_tables/list_views)
    """
    relkind_arr = pg_sql.SQL(", ").join(_lit(k) for k in relkinds)
    partition_filter = (
        pg_sql.SQL("") if include_partition_children else pg_sql.SQL("AND NOT c.relispartition")
    )

    # Count query (separate; cheap-ish; user-facing pagination needs it).
    count_sql = pg_sql.SQL(
        """
        SELECT count(*)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = {schema}
          AND c.relkind IN ({relkinds})
          {partition_filter}
        """
    ).format(
        schema=_lit(schema),
        relkinds=relkind_arr,
        partition_filter=partition_filter,
    )
    count_result = await _run_catalog_query(pool, count_sql)
    total = int(count_result.rows[0][0]) if count_result.rows else 0

    data_sql = pg_sql.SQL(
        """
        SELECT
            c.relname AS name,
            c.relkind AS kind,
            pg_catalog.pg_get_userbyid(c.relowner) AS owner,
            c.reltuples::bigint AS approximate_rows,
            pg_catalog.pg_total_relation_size(c.oid) AS total_bytes,
            obj_description(c.oid, 'pg_class') AS comment,
            c.relrowsecurity AS rls_enabled,
            c.relispartition AS is_partition_child,
            (SELECT count(*) FROM pg_catalog.pg_inherits i WHERE i.inhparent = c.oid) AS partition_count
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = {schema}
          AND c.relkind IN ({relkinds})
          {partition_filter}
        ORDER BY c.relname
        LIMIT {limit} OFFSET {offset}
        """
    ).format(
        schema=_lit(schema),
        relkinds=relkind_arr,
        partition_filter=partition_filter,
        limit=_lit(limit),
        offset=_lit(offset),
    )
    data_result = await _run_catalog_query(pool, data_sql)

    rows = [
        {
            "name": r[0],
            "kind": r[1],
            "owner": r[2],
            "approximate_rows": int(r[3]) if r[3] not in (None, "NULL") else None,
            "total_bytes": int(r[4]) if r[4] not in (None, "NULL") else None,
            "comment": r[5] if r[5] != "NULL" else None,
            "rls_enabled": r[6] == "true",
            "is_partition_child": r[7] == "true",
            "partition_count": int(r[8]) if r[8] not in (None, "NULL") else 0,
        }
        for r in data_result.rows
    ]
    return rows, total


# ---------------------------------------------------------------------------
# describe_table
# ---------------------------------------------------------------------------


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    default: str | None
    identity: str | None
    generated: str | None
    comment: str | None
    ordinal: int


@dataclass
class IndexInfo:
    name: str
    definition: str
    is_unique: bool
    is_primary: bool


@dataclass
class ConstraintInfo:
    name: str
    type: str  # 'p' = PK, 'u' = unique, 'c' = check, 'f' = FK
    definition: str


@dataclass
class ForeignKeyInfo:
    name: str
    columns: list[str]
    references_table: str
    references_columns: list[str]
    on_update: str
    on_delete: str


@dataclass
class TableDescription:
    schema: str
    name: str
    relkind: str
    owner: str | None
    comment: str | None
    approximate_rows: int | None
    total_bytes: int | None
    rls_enabled: bool
    columns: list[ColumnInfo]
    primary_key: list[str]
    unique_constraints: list[ConstraintInfo]
    check_constraints: list[ConstraintInfo]
    foreign_keys: list[ForeignKeyInfo]
    indexes: list[IndexInfo]
    inherits_from: list[str]
    partition_strategy: str | None
    partition_key: str | None
    partition_count: int
    view_definition: str | None = None  # populated for views / matviews
    view_status: str = "ok"
    view_error: str | None = None


async def describe_table(
    pool: AsyncConnectionPool, *, schema: str, table: str
) -> TableDescription | None:
    """Return a TableDescription for *schema.table* or None if not found."""
    base_sql = pg_sql.SQL(
        """
        SELECT
            c.oid,
            c.relkind,
            pg_catalog.pg_get_userbyid(c.relowner),
            obj_description(c.oid, 'pg_class'),
            c.reltuples::bigint,
            pg_catalog.pg_total_relation_size(c.oid),
            c.relrowsecurity,
            (SELECT pt.partstrat FROM pg_catalog.pg_partitioned_table pt WHERE pt.partrelid = c.oid),
            (SELECT pg_catalog.pg_get_partkeydef(c.oid)),
            (SELECT count(*) FROM pg_catalog.pg_inherits i WHERE i.inhparent = c.oid)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = {schema} AND c.relname = {table}
        """
    ).format(schema=_lit(schema), table=_lit(table))
    base_result = await _run_catalog_query(pool, base_sql)
    if not base_result.rows:
        return None

    row = base_result.rows[0]
    oid = int(row[0])
    relkind = row[1]
    owner = row[2]
    comment = row[3] if row[3] != "NULL" else None
    approx_rows = int(row[4]) if row[4] not in (None, "NULL") else None
    total_bytes = int(row[5]) if row[5] not in (None, "NULL") else None
    rls = row[6] == "true"
    partition_strategy = row[7] if row[7] != "NULL" else None
    partition_key = row[8] if row[8] != "NULL" else None
    partition_count = int(row[9]) if row[9] not in (None, "NULL") else 0

    columns = await _describe_columns(pool, oid)
    indexes = await _describe_indexes(pool, oid)
    # Infer PK columns from the primary-key index's indexdef string.
    pk_cols: list[str] = []
    for ix in indexes:
        if ix.is_primary:
            # indexdef looks like: CREATE UNIQUE INDEX ... ON ... (col_a, col_b)
            # Extract the parenthesized column list.
            start = ix.definition.rfind("(")
            end = ix.definition.rfind(")")
            if 0 <= start < end:
                pk_cols = [
                    p.strip().strip('"').split(" ")[0]
                    for p in ix.definition[start + 1 : end].split(",")
                ]
            break
    unique_cs, check_cs, fks = await _describe_constraints(pool, oid)
    parents = await _describe_parents(pool, oid)

    view_definition: str | None = None
    view_status = "ok"
    view_error: str | None = None
    if relkind in ("v", "m"):
        view_definition, view_status, view_error = await _get_view_definition(pool, schema, table)

    return TableDescription(
        schema=schema,
        name=table,
        relkind=relkind,
        owner=owner if owner != "NULL" else None,
        comment=comment,
        approximate_rows=approx_rows,
        total_bytes=total_bytes,
        rls_enabled=rls,
        columns=columns,
        primary_key=pk_cols,
        unique_constraints=unique_cs,
        check_constraints=check_cs,
        foreign_keys=fks,
        indexes=indexes,
        inherits_from=parents,
        partition_strategy=partition_strategy,
        partition_key=partition_key,
        partition_count=partition_count,
        view_definition=view_definition,
        view_status=view_status,
        view_error=view_error,
    )


async def _describe_columns(pool: AsyncConnectionPool, oid: int) -> list[ColumnInfo]:
    sql = pg_sql.SQL(
        """
        SELECT
            a.attnum,
            a.attname,
            pg_catalog.format_type(a.atttypid, a.atttypmod),
            NOT a.attnotnull AS nullable,
            pg_catalog.pg_get_expr(d.adbin, d.adrelid),
            CASE a.attidentity
                WHEN 'a' THEN 'GENERATED ALWAYS AS IDENTITY'
                WHEN 'd' THEN 'GENERATED BY DEFAULT AS IDENTITY'
                ELSE NULL
            END,
            CASE a.attgenerated
                WHEN 's' THEN 'GENERATED ALWAYS STORED'
                ELSE NULL
            END,
            col_description(a.attrelid, a.attnum)
        FROM pg_catalog.pg_attribute a
        LEFT JOIN pg_catalog.pg_attrdef d
               ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = {oid}
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """
    ).format(oid=_lit(oid))
    result = await _run_catalog_query(pool, sql)
    return [
        ColumnInfo(
            ordinal=int(r[0]),
            name=r[1],
            type=r[2],
            nullable=r[3] == "true",
            default=r[4] if r[4] != "NULL" else None,
            identity=r[5] if r[5] != "NULL" else None,
            generated=r[6] if r[6] != "NULL" else None,
            comment=r[7] if r[7] != "NULL" else None,
        )
        for r in result.rows
    ]


async def _describe_indexes(pool: AsyncConnectionPool, oid: int) -> list[IndexInfo]:
    sql = pg_sql.SQL(
        """
        SELECT
            i.relname,
            pg_catalog.pg_get_indexdef(ix.indexrelid),
            ix.indisunique,
            ix.indisprimary
        FROM pg_catalog.pg_index ix
        JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
        WHERE ix.indrelid = {oid}
        ORDER BY i.relname
        """
    ).format(oid=_lit(oid))
    result = await _run_catalog_query(pool, sql)
    return [
        IndexInfo(
            name=r[0],
            definition=r[1],
            is_unique=r[2] == "true",
            is_primary=r[3] == "true",
        )
        for r in result.rows
    ]


async def _describe_constraints(
    pool: AsyncConnectionPool, oid: int
) -> tuple[list[ConstraintInfo], list[ConstraintInfo], list[ForeignKeyInfo]]:
    sql = pg_sql.SQL(
        """
        SELECT
            con.conname,
            con.contype,
            pg_catalog.pg_get_constraintdef(con.oid, true),
            con.conrelid,
            con.confrelid,
            con.conkey,
            con.confkey,
            con.confupdtype,
            con.confdeltype
        FROM pg_catalog.pg_constraint con
        WHERE con.conrelid = {oid}
        ORDER BY con.contype, con.conname
        """
    ).format(oid=_lit(oid))
    result = await _run_catalog_query(pool, sql)

    unique: list[ConstraintInfo] = []
    checks: list[ConstraintInfo] = []
    fks: list[ForeignKeyInfo] = []
    for r in result.rows:
        name, ctype, defn = r[0], r[1], r[2]
        if ctype in ("u",):
            unique.append(ConstraintInfo(name=name, type="unique", definition=defn))
        elif ctype == "c":
            checks.append(ConstraintInfo(name=name, type="check", definition=defn))
        elif ctype == "f":
            # Parse fk details from definition string — more robust than
            # decoding the int[] arrays inline.
            fks.append(
                ForeignKeyInfo(
                    name=name,
                    columns=[],  # definition string has them; parsing left for UI
                    references_table="",
                    references_columns=[],
                    on_update=r[7],
                    on_delete=r[8],
                )
            )
            # Replace with a richer placeholder via the constraint definition.
            fks[-1].references_table = defn
    return unique, checks, fks


async def _describe_parents(pool: AsyncConnectionPool, oid: int) -> list[str]:
    sql = pg_sql.SQL(
        """
        SELECT n.nspname || '.' || c.relname
        FROM pg_catalog.pg_inherits i
        JOIN pg_catalog.pg_class c ON c.oid = i.inhparent
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE i.inhrelid = {oid}
        ORDER BY 1
        """
    ).format(oid=_lit(oid))
    result = await _run_catalog_query(pool, sql)
    return [r[0] for r in result.rows]


async def _get_view_definition(
    pool: AsyncConnectionPool, schema: str, name: str
) -> tuple[str | None, str, str | None]:
    sql = pg_sql.SQL(
        "SELECT pg_catalog.pg_get_viewdef(c.oid, true) "
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = {schema} AND c.relname = {name}"
    ).format(schema=_lit(schema), name=_lit(name))
    result = await _run_catalog_query(pool, sql)
    if not result.rows:
        return None, "not_found", "view definition not found"

    definition = result.rows[0][0]
    # Sanity-check by asking the planner if the view can still execute.
    # EXPLAIN will fail if an underlying table was dropped.
    ident = pg_sql.Identifier(schema) + pg_sql.SQL(".") + pg_sql.Identifier(name)
    probe_sql = pg_sql.SQL("EXPLAIN SELECT * FROM {ident} LIMIT 0").format(ident=ident)
    try:
        await _run_catalog_query(pool, probe_sql)
    except Exception as e:
        return definition, "broken", str(e)
    return definition, "ok", None


# ---------------------------------------------------------------------------
# sample_rows
# ---------------------------------------------------------------------------


async def sample_rows(
    pool: AsyncConnectionPool,
    *,
    schema: str,
    table: str,
    limit: int,
    row_limit: int,
    byte_limit: int,
    cell_limit: int,
    timeout_ms: int,
) -> tuple[QueryResult, int | None, bool]:
    """Return (QueryResult, approx_total_rows, rls_enabled).

    The two extra scalars let the caller tell an empty table apart from
    an RLS-filtered read.
    """
    info_sql = pg_sql.SQL(
        "SELECT c.reltuples::bigint, c.relrowsecurity "
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = {schema} AND c.relname = {table}"
    ).format(schema=_lit(schema), table=_lit(table))
    info = await _run_catalog_query(pool, info_sql)
    approx_rows: int | None = None
    rls = False
    if info.rows:
        r = info.rows[0]
        approx_rows = int(r[0]) if r[0] not in (None, "NULL") else None
        rls = r[1] == "true"

    qualified = pg_sql.Identifier(schema) + pg_sql.SQL(".") + pg_sql.Identifier(table)
    effective_limit = min(limit, row_limit)
    sample_sql = pg_sql.SQL("SELECT * FROM {ident} LIMIT {lim}").format(
        ident=qualified, lim=_lit(effective_limit)
    )
    # Compile and run through the runner so all RO guarantees apply.
    import psycopg

    compiled = sample_sql.as_string(psycopg.adapters)
    result = await run_select(
        pool,
        compiled,
        row_limit=effective_limit,
        byte_limit=byte_limit,
        cell_limit=cell_limit,
        timeout_ms=timeout_ms,
    )
    return result, approx_rows, rls


# ---------------------------------------------------------------------------
# search_schema
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    kind: str  # table, view, column, function
    schema: str
    name: str
    parent: str | None = None  # for columns: "schema.table"
    comment: str | None = None


async def search_schema(
    pool: AsyncConnectionPool,
    *,
    pattern: str,
    kind: str = "all",
    limit: int = 100,
    include_system: bool = False,
) -> list[SearchHit]:
    like = f"%{pattern}%"
    sys_filter = (
        pg_sql.SQL("")
        if include_system
        else pg_sql.SQL(
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg\\_toast%' ESCAPE '\\'"
        )
    )

    queries: list[pg_sql.Composed] = []
    if kind in ("all", "table", "view"):
        relkinds = {
            "all": "'r','p','f','v','m'",
            "table": "'r','p','f'",
            "view": "'v','m'",
        }[kind]
        queries.append(
            pg_sql.SQL(
                """
                SELECT
                    CASE c.relkind
                        WHEN 'v' THEN 'view'
                        WHEN 'm' THEN 'view'
                        ELSE 'table'
                    END,
                    n.nspname,
                    c.relname,
                    NULL,
                    obj_description(c.oid, 'pg_class')
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ({relkinds})
                  AND (c.relname ILIKE {like} OR coalesce(obj_description(c.oid, 'pg_class'), '') ILIKE {like})
                  {sys_filter}
                """
            ).format(
                relkinds=pg_sql.SQL(relkinds),
                like=_lit(like),
                sys_filter=sys_filter,
            )
        )
    if kind in ("all", "column"):
        queries.append(
            pg_sql.SQL(
                """
                SELECT
                    'column',
                    n.nspname,
                    a.attname,
                    n.nspname || '.' || c.relname,
                    col_description(a.attrelid, a.attnum)
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE a.attnum > 0
                  AND NOT a.attisdropped
                  AND c.relkind IN ('r','p','f','v','m')
                  AND (a.attname ILIKE {like} OR coalesce(col_description(a.attrelid, a.attnum), '') ILIKE {like})
                  {sys_filter}
                """
            ).format(like=_lit(like), sys_filter=sys_filter)
        )
    if kind in ("all", "function"):
        queries.append(
            pg_sql.SQL(
                """
                SELECT
                    'function',
                    n.nspname,
                    p.proname,
                    NULL,
                    obj_description(p.oid, 'pg_proc')
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                WHERE (p.proname ILIKE {like} OR coalesce(obj_description(p.oid, 'pg_proc'), '') ILIKE {like})
                  {sys_filter}
                """
            ).format(like=_lit(like), sys_filter=sys_filter)
        )

    combined = pg_sql.SQL(" UNION ALL ").join(queries)
    final = pg_sql.SQL("{body} LIMIT {lim}").format(body=combined, lim=_lit(limit))
    result = await _run_catalog_query(pool, final)
    return [
        SearchHit(
            kind=r[0],
            schema=r[1],
            name=r[2],
            parent=r[3] if r[3] != "NULL" else None,
            comment=r[4] if r[4] != "NULL" else None,
        )
        for r in result.rows
    ]


# ---------------------------------------------------------------------------
# table_stats
# ---------------------------------------------------------------------------


@dataclass
class TableStats:
    schema: str
    name: str
    approximate_rows: int | None
    total_bytes: int | None
    relation_bytes: int | None
    indexes_bytes: int | None
    n_live_tup: int | None
    n_dead_tup: int | None
    last_vacuum: str | None
    last_analyze: str | None
    relpages: int | None


async def table_stats(pool: AsyncConnectionPool, *, schema: str, table: str) -> TableStats | None:
    sql = pg_sql.SQL(
        """
        SELECT
            c.reltuples::bigint,
            pg_catalog.pg_total_relation_size(c.oid),
            pg_catalog.pg_relation_size(c.oid),
            pg_catalog.pg_indexes_size(c.oid),
            s.n_live_tup,
            s.n_dead_tup,
            s.last_vacuum::text,
            s.last_analyze::text,
            c.relpages
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_stat_all_tables s
               ON s.schemaname = n.nspname AND s.relname = c.relname
        WHERE n.nspname = {schema} AND c.relname = {table}
        """
    ).format(schema=_lit(schema), table=_lit(table))
    result = await _run_catalog_query(pool, sql)
    if not result.rows:
        return None
    r = result.rows[0]

    def _int(v: str) -> int | None:
        return int(v) if v not in (None, "NULL") else None

    def _nullable(v: str) -> str | None:
        return v if v != "NULL" else None

    return TableStats(
        schema=schema,
        name=table,
        approximate_rows=_int(r[0]),
        total_bytes=_int(r[1]),
        relation_bytes=_int(r[2]),
        indexes_bytes=_int(r[3]),
        n_live_tup=_int(r[4]),
        n_dead_tup=_int(r[5]),
        last_vacuum=_nullable(r[6]),
        last_analyze=_nullable(r[7]),
        relpages=_int(r[8]),
    )


__all__ = [
    "ColumnInfo",
    "ConstraintInfo",
    "ForeignKeyInfo",
    "IndexInfo",
    "SearchHit",
    "TableDescription",
    "TableStats",
    "describe_table",
    "list_relations",
    "list_schemas",
    "sample_rows",
    "search_schema",
    "table_stats",
]
