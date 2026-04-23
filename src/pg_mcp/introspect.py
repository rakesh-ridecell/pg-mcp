"""Catalog queries powering the introspection tools.

Every query runs inside a ``READ ONLY`` transaction. We use
``pg_catalog`` directly (not ``information_schema``) for speed and
precision — ``information_schema`` has permission-aware filtering that
can hide objects the RO role can in fact see via SELECT.

All user-supplied identifiers are embedded via
:class:`psycopg.sql.Literal` / :class:`psycopg.sql.Identifier`. We
never string-format raw user input into SQL.

Performance note
~~~~~~~~~~~~~~~~
Catalog queries use a lightweight path (:func:`_catalog_session` /
:func:`_run_catalog_sql`) that executes directly via ``cur.execute`` +
``cur.fetchall()`` rather than the server-side cursor used by user
queries. Tools that need several catalog queries (like
:func:`describe_table`) open one :func:`_catalog_session` and run all
their queries inside it — cutting round-trips from ~9-per-query to
~2-per-query and amortizing the transaction setup. On a remote DB
with ~100ms latency this is the difference between a ~15-second
``describe_table`` and a sub-second one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
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
# Catalog execution helpers
# ---------------------------------------------------------------------------

# Default catalog timeout — catalog queries are small and should be
# quick. If a catalog query takes longer than this something is wrong
# (huge schema, heavy lock wait, …).
CATALOG_TIMEOUT_MS = 15_000


@asynccontextmanager
async def _catalog_session(
    pool: AsyncConnectionPool,
    *,
    timeout_ms: int = CATALOG_TIMEOUT_MS,
) -> AsyncIterator[psycopg.AsyncCursor]:
    """Yield a cursor inside a shared READ ONLY transaction.

    Use this when a tool needs several catalog queries — sharing the
    transaction eliminates per-query BEGIN/SET/ROLLBACK overhead.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        await conn.execute("SET TRANSACTION READ ONLY")
        async with conn.cursor() as cur:
            yield cur


async def _fetchall(cur: psycopg.AsyncCursor, sql: str | pg_sql.Composed) -> list[tuple]:
    """Execute *sql* on *cur* and return all rows. Empty list on no result set."""
    await cur.execute(sql)
    if cur.description is None:
        return []
    return await cur.fetchall()


async def _fetchone(cur: psycopg.AsyncCursor, sql: str | pg_sql.Composed) -> tuple | None:
    await cur.execute(sql)
    if cur.description is None:
        return None
    return await cur.fetchone()


async def _run_catalog_sql(
    pool: AsyncConnectionPool,
    sql: str | pg_sql.Composed,
    *,
    timeout_ms: int = CATALOG_TIMEOUT_MS,
) -> list[tuple]:
    """Single-shot catalog query. Opens its own RO session."""
    async with _catalog_session(pool, timeout_ms=timeout_ms) as cur:
        return await _fetchall(cur, sql)


def _as_bool(value: Any) -> bool:
    """Coerce a raw catalog value to bool (handles Python bool and None)."""
    return bool(value) if value is not None else False


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


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
    rows = await _run_catalog_sql(pool, sql)
    return [
        {
            "name": r[0],
            "owner": _as_str_or_none(r[1]),
            "comment": _as_str_or_none(r[2]),
        }
        for r in rows
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

    # Single shared transaction for count + data — cuts round-trips in half.
    async with _catalog_session(pool) as cur:
        count_rows = await _fetchall(cur, count_sql)
        total = _as_int(count_rows[0][0]) if count_rows else 0
        data_rows = await _fetchall(cur, data_sql)

    rows = [
        {
            "name": r[0],
            "kind": r[1],
            "owner": _as_str_or_none(r[2]),
            "approximate_rows": _as_int(r[3]),
            "total_bytes": _as_int(r[4]),
            "comment": _as_str_or_none(r[5]),
            "rls_enabled": _as_bool(r[6]),
            "is_partition_child": _as_bool(r[7]),
            "partition_count": _as_int(r[8]) or 0,
        }
        for r in data_rows
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
    columns: list[str]  # key columns in order (excludes INCLUDE)


@dataclass
class ConstraintInfo:
    name: str
    type: str  # 'unique', 'check' (PK and FK have their own types)
    definition: str


@dataclass
class ForeignKeyInfo:
    name: str
    columns: list[str]  # local columns
    references_table: str  # 'schema.table'
    references_columns: list[str]  # referenced columns
    on_update: str  # "NO ACTION" | "CASCADE" | ...
    on_delete: str
    definition: str = ""  # full pg_get_constraintdef output


_FK_ACTION_CODES = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _fk_action(code: Any) -> str:
    if code is None:
        return ""
    return _FK_ACTION_CODES.get(str(code), str(code))


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
    """Return a TableDescription for *schema.table*, or None if not found.

    Runs all ~5 catalog queries inside a single shared RO transaction so
    total latency is bounded by ``2 + N`` round-trips instead of
    ``9 * N``. On a 100ms-latency link this is the difference between
    a ~1s and a ~6s response.
    """
    # ---- 1. Base relation info (is this even a table/view/etc we can see?)
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

    columns_sql_tpl = pg_sql.SQL(
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
    )
    indexes_sql_tpl = pg_sql.SQL(
        """
        SELECT
            i.relname,
            pg_catalog.pg_get_indexdef(ix.indexrelid),
            ix.indisunique,
            ix.indisprimary,
            -- Column names for the index in key order (excludes INCLUDE columns).
            ARRAY(
                SELECT a.attname
                FROM unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = ix.indrelid AND a.attnum = k.attnum
                WHERE k.ord <= ix.indnkeyatts
                ORDER BY k.ord
            ) AS key_columns
        FROM pg_catalog.pg_index ix
        JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
        WHERE ix.indrelid = {oid}
        ORDER BY i.relname
        """
    )
    # Constraints — for foreign keys, also resolve the referenced schema,
    # table, and column names so ForeignKeyInfo can be populated cleanly.
    constraints_sql_tpl = pg_sql.SQL(
        """
        SELECT
            con.conname,
            con.contype,
            pg_catalog.pg_get_constraintdef(con.oid, true),
            con.confupdtype,
            con.confdeltype,
            -- Owning columns (NULL for checks/etc.)
            ARRAY(
                SELECT a.attname
                FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                ORDER BY k.ord
            ) AS conkey_names,
            -- Referenced table (FK only)
            CASE WHEN con.contype = 'f' THEN (
                SELECT rn.nspname || '.' || rc.relname
                FROM pg_catalog.pg_class rc
                JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace
                WHERE rc.oid = con.confrelid
            ) END AS confrel_qualified,
            -- Referenced columns (FK only)
            CASE WHEN con.contype = 'f' THEN ARRAY(
                SELECT a.attname
                FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = con.confrelid AND a.attnum = k.attnum
                ORDER BY k.ord
            ) END AS confkey_names
        FROM pg_catalog.pg_constraint con
        WHERE con.conrelid = {oid}
        ORDER BY con.contype, con.conname
        """
    )
    parents_sql_tpl = pg_sql.SQL(
        """
        SELECT n.nspname || '.' || c.relname
        FROM pg_catalog.pg_inherits i
        JOIN pg_catalog.pg_class c ON c.oid = i.inhparent
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE i.inhrelid = {oid}
        ORDER BY 1
        """
    )
    view_def_sql_tpl = pg_sql.SQL("SELECT pg_catalog.pg_get_viewdef({oid}, true)")

    async with _catalog_session(pool) as cur:
        base_row = await _fetchone(cur, base_sql)
        if base_row is None:
            return None

        oid = int(base_row[0])
        relkind = base_row[1]
        owner = _as_str_or_none(base_row[2])
        comment = _as_str_or_none(base_row[3])
        approx_rows = _as_int(base_row[4])
        total_bytes = _as_int(base_row[5])
        rls = _as_bool(base_row[6])
        partition_strategy = _as_str_or_none(base_row[7])
        partition_key = _as_str_or_none(base_row[8])
        partition_count = _as_int(base_row[9]) or 0

        # ---- 2. Columns
        col_rows = await _fetchall(cur, columns_sql_tpl.format(oid=_lit(oid)))
        columns = [
            ColumnInfo(
                ordinal=int(r[0]),
                name=r[1],
                type=r[2],
                nullable=_as_bool(r[3]),
                default=_as_str_or_none(r[4]),
                identity=_as_str_or_none(r[5]),
                generated=_as_str_or_none(r[6]),
                comment=_as_str_or_none(r[7]),
            )
            for r in col_rows
        ]

        # ---- 3. Indexes
        ix_rows = await _fetchall(cur, indexes_sql_tpl.format(oid=_lit(oid)))
        indexes = [
            IndexInfo(
                name=r[0],
                definition=r[1],
                is_unique=_as_bool(r[2]),
                is_primary=_as_bool(r[3]),
                columns=list(r[4] or ()),
            )
            for r in ix_rows
        ]

        # PK cols: read from the PK index's column list (reliable even
        # for expression indexes and INCLUDE columns).
        pk_cols: list[str] = next(
            (ix.columns for ix in indexes if ix.is_primary),
            [],
        )

        # ---- 4. Constraints (FK fields fully resolved from pg_constraint)
        con_rows = await _fetchall(cur, constraints_sql_tpl.format(oid=_lit(oid)))
        unique_cs: list[ConstraintInfo] = []
        check_cs: list[ConstraintInfo] = []
        fks: list[ForeignKeyInfo] = []
        for r in con_rows:
            name, ctype, defn = r[0], r[1], r[2]
            if ctype == "u":
                unique_cs.append(ConstraintInfo(name=name, type="unique", definition=defn))
            elif ctype == "c":
                check_cs.append(ConstraintInfo(name=name, type="check", definition=defn))
            elif ctype == "f":
                fks.append(
                    ForeignKeyInfo(
                        name=name,
                        columns=list(r[5] or ()),
                        references_table=_as_str_or_none(r[6]) or "",
                        references_columns=list(r[7] or ()),
                        on_update=_fk_action(r[3]),
                        on_delete=_fk_action(r[4]),
                        definition=defn,
                    )
                )

        # ---- 5. Inheritance parents
        parent_rows = await _fetchall(cur, parents_sql_tpl.format(oid=_lit(oid)))
        parents = [r[0] for r in parent_rows]

        # ---- 6. View definition (only for relkind v/m)
        view_definition: str | None = None
        view_status = "ok"
        view_error: str | None = None
        if relkind in ("v", "m"):
            try:
                vd_row = await _fetchone(cur, view_def_sql_tpl.format(oid=_lit(oid)))
                view_definition = vd_row[0] if vd_row else None
            except psycopg.Error as e:
                view_status = "broken"
                view_error = str(e)

    return TableDescription(
        schema=schema,
        name=table,
        relkind=relkind,
        owner=owner,
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
    info_rows = await _run_catalog_sql(pool, info_sql)
    approx_rows: int | None = None
    rls = False
    if info_rows:
        approx_rows = _as_int(info_rows[0][0])
        rls = _as_bool(info_rows[0][1])

    qualified = pg_sql.Identifier(schema) + pg_sql.SQL(".") + pg_sql.Identifier(table)
    effective_limit = min(limit, row_limit)
    sample_sql = pg_sql.SQL("SELECT * FROM {ident} LIMIT {lim}").format(
        ident=qualified, lim=_lit(effective_limit)
    )
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
    rows = await _run_catalog_sql(pool, final)
    return [
        SearchHit(
            kind=r[0],
            schema=r[1],
            name=r[2],
            parent=_as_str_or_none(r[3]),
            comment=_as_str_or_none(r[4]),
        )
        for r in rows
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
    rows = await _run_catalog_sql(pool, sql)
    if not rows:
        return None
    r = rows[0]
    return TableStats(
        schema=schema,
        name=table,
        approximate_rows=_as_int(r[0]),
        total_bytes=_as_int(r[1]),
        relation_bytes=_as_int(r[2]),
        indexes_bytes=_as_int(r[3]),
        n_live_tup=_as_int(r[4]),
        n_dead_tup=_as_int(r[5]),
        last_vacuum=_as_str_or_none(r[6]),
        last_analyze=_as_str_or_none(r[7]),
        relpages=_as_int(r[8]),
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
