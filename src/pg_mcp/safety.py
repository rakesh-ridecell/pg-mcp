"""SQL safety gate: enforce read-only via pglast AST allow-list + deny-list.

This is the **third** layer of defense-in-depth (after Postgres role
grants and ``READ ONLY`` transaction). It is designed to fail closed:
the allow-list of *top-level* statement types is narrow, and a visitor
recursively rejects any DML or dangerous node anywhere in the AST —
including inside CTEs, scalar subqueries, ``EXPLAIN`` inner queries, or
set-operation arms.

A function deny-list catches side-effect-ful calls that are legal
``SELECT``\\ s to the parser (``pg_read_file``, ``pg_advisory_lock``,
``nextval``, ``dblink_exec``, …). The check looks at the *unqualified*
name so ``pg_catalog.pg_read_file`` is also blocked.
"""

from __future__ import annotations

from pglast import ast, parse_sql
from pglast.visitors import Visitor

from pg_mcp.errors import PolicyViolation

# Top-level statement types that are allowed. Everything else is rejected
# by class check, regardless of inner contents.
ALLOWED_TOP: tuple[type, ...] = (
    ast.SelectStmt,
    ast.ExplainStmt,
    ast.VariableShowStmt,
)


# Node types that must NOT appear anywhere in the AST.
#
# We enumerate them rather than using a fallthrough to keep the guard
# explicit about what is denied, and to surface the offending type name
# in the violation message.
def _forbidden_node_types() -> tuple[type, ...]:
    candidates = [
        "InsertStmt",
        "UpdateStmt",
        "DeleteStmt",
        "MergeStmt",
        "TruncateStmt",
        "CopyStmt",
        "CreateStmt",
        "DropStmt",
        "AlterTableStmt",
        "AlterDatabaseStmt",
        "AlterRoleStmt",
        "AlterObjectSchemaStmt",
        "AlterOwnerStmt",
        "AlterFunctionStmt",
        "AlterSeqStmt",
        "VacuumStmt",
        "ClusterStmt",
        "ReindexStmt",
        "DoStmt",
        "CallStmt",
        "GrantStmt",
        "RevokeStmt",
        "GrantRoleStmt",
        "TransactionStmt",
        "LockStmt",
        "NotifyStmt",
        "ListenStmt",
        "UnlistenStmt",
        "RefreshMatViewStmt",
        "PrepareStmt",
        "ExecuteStmt",
        "DeallocateStmt",
        "CreateTableAsStmt",
        "VariableSetStmt",
        "LoadStmt",
        "CheckPointStmt",
        "CreateFunctionStmt",
        "CreateTrigStmt",
        "CreateRoleStmt",
        "DropRoleStmt",
        "CreatePolicyStmt",
        "AlterPolicyStmt",
        "DropOwnedStmt",
        "ReassignOwnedStmt",
        "SecLabelStmt",
        "ImportForeignSchemaStmt",
        "CreateExtensionStmt",
        "AlterExtensionStmt",
        "CreateForeignTableStmt",
    ]
    resolved: list[type] = []
    for name in candidates:
        node_type = getattr(ast, name, None)
        if node_type is not None:
            resolved.append(node_type)
    return tuple(resolved)


FORBIDDEN_ANYWHERE: tuple[type, ...] = _forbidden_node_types()


# Unqualified, lowercased function names that must be rejected even
# though the surrounding statement is a SELECT.
FUNCTION_DENYLIST: frozenset[str] = frozenset(
    {
        # Filesystem access
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_ls_logdir",
        "pg_ls_waldir",
        "pg_ls_archive_statusdir",
        "pg_ls_tmpdir",
        "lo_export",
        "lo_import",
        "lo_put",
        "lo_get",
        "lo_from_bytea",
        "lo_creat",
        "lo_create",
        # Cross-database exfiltration / writes
        "dblink",
        "dblink_exec",
        "dblink_send_query",
        "dblink_connect",
        "dblink_connect_u",
        # Locks & NOTIFY (NOT blocked by RO txn)
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_shared",
        "pg_advisory_unlock_all",
        "pg_notify",
        # Backend / server control
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_logical_emit_message",
        "pg_promote",
        # Session state mutation
        "set_config",
        # Sequences (writes!)
        "nextval",
        "setval",
        # Replication
        "pg_create_logical_replication_slot",
        "pg_drop_replication_slot",
        "pg_replication_slot_advance",
        "pg_create_physical_replication_slot",
        "pg_export_snapshot",
        "pg_import_snapshot",
    }
)

# Any function whose name starts with one of these prefixes is blocked.
# Catches `pg_ls_*` variants added in future PG versions without a code
# change here.
FUNCTION_DENYLIST_PREFIXES: tuple[str, ...] = (
    "pg_ls_",
    "lo_",
    "dblink",
)


def _funccall_name(funccall: ast.FuncCall) -> str:
    """Return the lowercased, unqualified function name from a FuncCall.

    ``funcname`` is a tuple of ``String`` nodes (e.g., ``pg_catalog``,
    ``pg_read_file``); we want the last one.
    """
    parts = funccall.funcname or ()
    if not parts:
        return ""
    last = parts[-1]
    # pglast String nodes expose the string value via ``.sval``.
    value = getattr(last, "sval", None) or getattr(last, "val", None) or ""
    return str(value).lower()


class _Guard(Visitor):
    """Walk the AST and record the first policy violation encountered.

    Uses an early-exit sentinel so we don't waste work after the first
    violation — the visitor cannot abort traversal directly, but subsequent
    ``visit`` calls become no-ops once ``violation`` is set.
    """

    def __init__(self) -> None:
        self.violation: PolicyViolation | None = None

    def visit(self, ancestors: object, node: object) -> None:  # type: ignore[override]
        # pglast's Visitor base class has no type stubs; we keep object
        # here for static analysis and rely on isinstance checks below.
        if self.violation is not None:
            return

        # Forbidden node types anywhere in the tree.
        if isinstance(node, FORBIDDEN_ANYWHERE):
            self.violation = PolicyViolation("disallowed_statement", type(node).__name__)
            return

        # SELECT INTO creates a table — reject.
        if isinstance(node, ast.SelectStmt) and node.intoClause is not None:
            self.violation = PolicyViolation("disallowed_statement", "SelectInto")
            return

        # EXPLAIN ANALYZE actually executes the inner statement. Reject
        # unless the inner is a plain SELECT (already covered by the
        # top-level allow-list check in assert_readonly).
        if isinstance(node, ast.ExplainStmt):
            is_analyze = False
            for opt in node.options or ():
                defname = (getattr(opt, "defname", None) or "").lower()
                if defname == "analyze":
                    is_analyze = True
                    break
            if is_analyze and not isinstance(node.query, ast.SelectStmt):
                self.violation = PolicyViolation(
                    "dml_in_explain_analyze",
                    type(node.query).__name__ if node.query else "None",
                )
                return

        # Function deny-list.
        if isinstance(node, ast.FuncCall):
            name = _funccall_name(node)
            if name in FUNCTION_DENYLIST or any(
                name.startswith(prefix) for prefix in FUNCTION_DENYLIST_PREFIXES
            ):
                self.violation = PolicyViolation("disallowed_function", name)
                return


# Maximum SQL string length accepted by the gate. Prevents DoS via
# pathologically deep/long input. Overridable per-call.
DEFAULT_MAX_SQL_LEN = 100_000


def assert_readonly(sql: str, *, max_len: int = DEFAULT_MAX_SQL_LEN) -> None:
    """Raise :class:`PolicyViolation` if *sql* is not provably read-only.

    The pipeline:

    1. Basic bounds checks (not empty, not oversized).
    2. Parse with pglast; reject if parse fails.
    3. Require exactly one statement (reject ``SELECT 1; DROP TABLE t``
       and comment-only input).
    4. Require the top-level node is in :data:`ALLOWED_TOP`.
    5. Walk the full AST with :class:`_Guard` to catch DML in CTEs,
       forbidden utility statements nested anywhere, ``EXPLAIN ANALYZE``
       of non-SELECT, and deny-listed function calls.
    """
    if not sql or not sql.strip():
        raise PolicyViolation("empty_sql", "")
    if len(sql) > max_len:
        raise PolicyViolation("sql_too_long", f"{len(sql)} > {max_len}")

    try:
        raws = parse_sql(sql)
    except Exception as e:
        raise PolicyViolation("sql_parse_error", str(e)) from e

    if len(raws) == 0:
        raise PolicyViolation("empty_sql", "comment-only input")
    if len(raws) > 1:
        raise PolicyViolation("multiple_statements_not_allowed", f"{len(raws)} statements")

    stmt = raws[0].stmt
    if not isinstance(stmt, ALLOWED_TOP):
        raise PolicyViolation("disallowed_statement", type(stmt).__name__)

    guard = _Guard()
    guard(raws)
    if guard.violation is not None:
        raise guard.violation


def extract_referenced_schemas(sql: str) -> set[str]:
    """Return the set of schema names explicitly referenced in *sql*.

    Walks the AST looking for ``RangeVar`` nodes (table references in
    ``FROM``, ``JOIN``, etc.) and collects their ``schemaname``. Tables
    referenced without a schema qualifier are NOT included in the
    result — the caller has to decide separately whether an
    unqualified reference is allowed (typically by treating the
    connection's ``search_path`` as the effective schema).

    Parse errors return an empty set; the caller should run
    :func:`assert_readonly` first to distinguish.
    """
    try:
        raws = parse_sql(sql)
    except Exception:
        return set()
    schemas: set[str] = set()

    class _Collector(Visitor):
        def visit(self, ancestors: object, node: object) -> None:  # type: ignore[override]
            if isinstance(node, ast.RangeVar):
                name = getattr(node, "schemaname", None)
                if name:
                    schemas.add(str(name).lower())

    _Collector()(raws)
    return schemas


class SchemaPolicy:
    """Allow-list / deny-list check for schema references.

    Instantiate once per connection with the config-level lists; reuse
    across requests. ``check()`` raises :class:`PolicyViolation` on any
    violation.
    """

    def __init__(
        self,
        *,
        allowed: list[str] | None = None,
        denied: list[str] | None = None,
    ) -> None:
        self.allowed: set[str] | None = {s.lower() for s in allowed} if allowed else None
        self.denied: set[str] = {s.lower() for s in (denied or ())}

    def is_allowed(self, schema: str) -> bool:
        name = schema.lower()
        if name in self.denied:
            return False
        return not (self.allowed is not None and name not in self.allowed)

    def check_sql(self, sql: str) -> None:
        """Raise ``PolicyViolation`` if *sql* references a disallowed schema."""
        if self.allowed is None and not self.denied:
            return
        for schema in extract_referenced_schemas(sql):
            if not self.is_allowed(schema):
                raise PolicyViolation("disallowed_schema", f"schema {schema!r} is not permitted")


__all__ = [
    "ALLOWED_TOP",
    "FORBIDDEN_ANYWHERE",
    "FUNCTION_DENYLIST",
    "FUNCTION_DENYLIST_PREFIXES",
    "SchemaPolicy",
    "assert_readonly",
    "extract_referenced_schemas",
]
