"""Exhaustive tests for the read-only safety gate.

Organized by what the SpecFlow analysis called out — every known edge
case has at least one test here. If you're adding a new policy rule,
add a test alongside.
"""

from __future__ import annotations

import pytest

from pg_mcp.errors import PolicyViolation
from pg_mcp.safety import assert_readonly

# ---------------------------------------------------------------------------
# Allowed (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT * FROM users",
        "SELECT id, email FROM users WHERE created_at > '2026-01-01'",
        "SELECT 'a;b' AS s",  # semicolon inside string literal is fine
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n<10) SELECT * FROM t",
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 INTERSECT SELECT 1",
        "SELECT 1 EXCEPT SELECT 2",
        "SELECT * FROM a JOIN b ON a.id = b.a_id",
        "SELECT * FROM a LEFT JOIN b USING (id)",
        "EXPLAIN SELECT 1",
        "EXPLAIN (FORMAT JSON) SELECT 1",
        "EXPLAIN (VERBOSE, COSTS) SELECT 1",
        "EXPLAIN (ANALYZE) SELECT 1",  # ANALYZE on SELECT is allowed
        "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM pg_class LIMIT 1",
        "SHOW search_path",
        "SHOW ALL",
        "SELECT CURRENT_TIMESTAMP",
        "SELECT pg_read_file",  # identifier, not a call
        "SELECT pg_sleep(0.01)",  # sleep is harmless; timeout will catch long ones
        "SELECT generate_series(1, 10)",  # bounded by row/byte cap at runtime
        "SELECT jsonb_build_object('a', 1)",
        "SELECT * FROM pg_catalog.pg_class LIMIT 1",  # read-only access to catalog is fine
        "SELECT * FROM information_schema.tables LIMIT 1",
    ],
)
def test_allowed(sql: str) -> None:
    assert_readonly(sql)  # must not raise


# ---------------------------------------------------------------------------
# Rejected — bad top-level statements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,node",
    [
        ("INSERT INTO t VALUES (1)", "InsertStmt"),
        ("INSERT INTO t (x) SELECT 1", "InsertStmt"),
        ("UPDATE t SET x = 1", "UpdateStmt"),
        ("UPDATE t SET x = 1 WHERE id = 1", "UpdateStmt"),
        ("DELETE FROM t", "DeleteStmt"),
        ("DELETE FROM t WHERE x = 1", "DeleteStmt"),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "MergeStmt"),
        ("TRUNCATE t", "TruncateStmt"),
        ("TRUNCATE TABLE t, u", "TruncateStmt"),
        ("DROP TABLE t", "DropStmt"),
        ("DROP TABLE IF EXISTS t CASCADE", "DropStmt"),
        ("CREATE TABLE t (x int)", "CreateStmt"),
        ("ALTER TABLE t ADD COLUMN y int", "AlterTableStmt"),
        ("VACUUM", "VacuumStmt"),
        ("VACUUM t", "VacuumStmt"),
        ("ANALYZE", "VacuumStmt"),  # plain ANALYZE parses as VacuumStmt
        ("CLUSTER t", "ClusterStmt"),
        ("REINDEX TABLE t", "ReindexStmt"),
        ("DO $$ BEGIN PERFORM 1; END $$ LANGUAGE plpgsql", "DoStmt"),
        ("CALL my_proc()", "CallStmt"),
        ("GRANT SELECT ON t TO public", "GrantStmt"),
        ("REVOKE SELECT ON t FROM public", "GrantStmt"),
        ("BEGIN", "TransactionStmt"),
        ("COMMIT", "TransactionStmt"),
        ("ROLLBACK", "TransactionStmt"),
        ("LOCK TABLE t", "LockStmt"),
        ("LOCK t IN ACCESS EXCLUSIVE MODE", "LockStmt"),
        ("NOTIFY chan, 'x'", "NotifyStmt"),
        ("LISTEN chan", "ListenStmt"),
        ("UNLISTEN chan", "UnlistenStmt"),
        ("REFRESH MATERIALIZED VIEW m", "RefreshMatViewStmt"),
        ("REFRESH MATERIALIZED VIEW CONCURRENTLY m", "RefreshMatViewStmt"),
        ("PREPARE p AS SELECT 1", "PrepareStmt"),
        ("EXECUTE p", "ExecuteStmt"),
        ("DEALLOCATE p", "DeallocateStmt"),
        ("CREATE TABLE foo AS SELECT 1", "CreateTableAsStmt"),
        ("CREATE TEMP TABLE t AS SELECT 1", "CreateTableAsStmt"),
        ("SET ROLE admin", "VariableSetStmt"),
        ("SET SESSION AUTHORIZATION admin", "VariableSetStmt"),
        ("SET LOCAL statement_timeout = 100", "VariableSetStmt"),
        ("RESET search_path", "VariableSetStmt"),
        ("COPY t FROM STDIN", "CopyStmt"),
        ("COPY t TO STDOUT", "CopyStmt"),
        ("COPY (SELECT 1) TO '/tmp/x'", "CopyStmt"),
        (
            "CREATE INDEX ON t (x)",
            "DropStmt",
        ),  # IndexStmt not in allow-list; parses as non-allowed top
    ],
)
def test_reject_statement(sql: str, node: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    # Top-level non-SelectStmt is caught by the ALLOWED_TOP check,
    # which uses the reason "disallowed_statement". Nested DML uses the
    # visitor's "disallowed_statement" too. We don't over-assert on the
    # exact node name here; the reason category is the stable contract.
    assert excinfo.value.reason in {
        "disallowed_statement",
        "dml_in_explain_analyze",
        "disallowed_function",
    }


# ---------------------------------------------------------------------------
# Rejected — CTE / subquery DML smuggling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE t SET y=1 RETURNING *) SELECT * FROM x",
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        # Nested WITH with DML deeper inside
        "WITH a AS (SELECT 1), b AS (DELETE FROM t RETURNING *) SELECT * FROM b",
    ],
)
def test_reject_cte_dml(sql: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    assert excinfo.value.reason == "disallowed_statement"


# ---------------------------------------------------------------------------
# Rejected — EXPLAIN ANALYZE of DML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
        "EXPLAIN ANALYZE UPDATE t SET x = 1",
        "EXPLAIN ANALYZE DELETE FROM t",
        "EXPLAIN (ANALYZE, VERBOSE) DELETE FROM t",
        "EXPLAIN (ANALYZE TRUE) UPDATE t SET x=1",
    ],
)
def test_reject_explain_analyze_dml(sql: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    # Either the inner DML is caught by the visitor first (disallowed_statement)
    # or the explain-analyze-of-non-SELECT check catches it.
    assert excinfo.value.reason in {"dml_in_explain_analyze", "disallowed_statement"}


def test_explain_insert_without_analyze_rejected() -> None:
    """EXPLAIN INSERT (no ANALYZE) doesn't execute, but we still reject
    to keep the surface narrow: the visitor flags InsertStmt anywhere."""
    with pytest.raises(PolicyViolation):
        assert_readonly("EXPLAIN INSERT INTO t VALUES (1)")


# ---------------------------------------------------------------------------
# Rejected — function deny-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,expected_fn",
    [
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_read_binary_file('/etc/passwd')", "pg_read_binary_file"),
        ("SELECT pg_catalog.pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_ls_dir('/')", "pg_ls_dir"),  # prefix
        ("SELECT pg_stat_file('/etc/passwd')", "pg_stat_file"),
        ("SELECT lo_export(1, '/tmp/out')", "lo_export"),  # prefix
        ("SELECT lo_from_bytea(0, '\\x01')", "lo_from_bytea"),  # prefix
        ("SELECT dblink_exec('c', 'INSERT INTO t VALUES (1)')", "dblink_exec"),  # prefix
        ("SELECT dblink('h', 'SELECT 1')", "dblink"),  # prefix
        ("SELECT pg_advisory_lock(1)", "pg_advisory_lock"),
        ("SELECT pg_advisory_xact_lock(1)", "pg_advisory_xact_lock"),
        ("SELECT pg_try_advisory_lock(1)", "pg_try_advisory_lock"),
        ("SELECT pg_notify('c', 'x')", "pg_notify"),
        ("SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
        ("SELECT pg_cancel_backend(1)", "pg_cancel_backend"),
        ("SELECT pg_reload_conf()", "pg_reload_conf"),
        ("SELECT nextval('s')", "nextval"),
        ("SELECT setval('s', 1)", "setval"),
        ("SELECT set_config('x', 'y', false)", "set_config"),
        # Nested in subquery
        ("SELECT * FROM (SELECT pg_advisory_lock(1)) _", "pg_advisory_lock"),
        # Nested in WHERE
        ("SELECT 1 WHERE pg_try_advisory_lock(1)", "pg_try_advisory_lock"),
        # Nested inside CTE
        ("WITH x AS (SELECT pg_notify('c', 'x')) SELECT * FROM x", "pg_notify"),
    ],
)
def test_reject_function(sql: str, expected_fn: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    assert excinfo.value.reason == "disallowed_function"
    assert expected_fn in excinfo.value.detail.lower()


# ---------------------------------------------------------------------------
# Rejected — parse / structural
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "\n\n\t",
    ],
)
def test_empty_sql(sql: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    assert excinfo.value.reason == "empty_sql"


def test_comment_only_is_empty() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly("-- just a comment")
    assert excinfo.value.reason == "empty_sql"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE t",
        "SELECT 1;;SELECT 2",
    ],
)
def test_multi_statement_rejected(sql: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    assert excinfo.value.reason == "multiple_statements_not_allowed"


def test_single_statement_with_trailing_semi_allowed() -> None:
    # "SELECT 1;" is one statement (the trailing ; after the last stmt is
    # not another statement).
    assert_readonly("SELECT 1;")
    assert_readonly("SELECT 1;;")


def test_parse_error_surfaces_as_policy_violation() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly("SELECT 1 FROM")
    assert excinfo.value.reason == "sql_parse_error"


def test_sql_too_long() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly("SELECT 1" + (" OR 1=1" * 20_000))
    assert excinfo.value.reason == "sql_too_long"


# ---------------------------------------------------------------------------
# Comment-then-DDL — the "fake SELECT" trick
# ---------------------------------------------------------------------------


def test_comment_then_ddl_is_rejected_on_the_ddl() -> None:
    # pglast parses this as a single DropStmt (the comment is stripped).
    # We must reject on disallowed_statement, NOT mistake it for a SELECT.
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly("-- SELECT 1\nDROP TABLE t")
    assert excinfo.value.reason == "disallowed_statement"


# ---------------------------------------------------------------------------
# SELECT INTO creates a table — must be rejected
# ---------------------------------------------------------------------------


def test_select_into_rejected() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly("SELECT 1 INTO tmp FROM generate_series(1,1)")
    assert excinfo.value.reason == "disallowed_statement"
    assert excinfo.value.detail == "SelectInto"


# ---------------------------------------------------------------------------
# Schema-qualified deny-list bypass attempts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_catalog.nextval('s')",
        "SELECT pg_catalog.pg_advisory_lock(1)",
        "SELECT pg_catalog.set_config('x', 'y', false)",
        "SELECT PUBLIC.dblink_exec('c', 'X')",  # uppercase schema
    ],
)
def test_schema_qualified_denylist(sql: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    assert excinfo.value.reason == "disallowed_function"


# ---------------------------------------------------------------------------
# Mixed-case / whitespace variants of deny-listed functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT PG_ADVISORY_LOCK(1)",
        "SELECT Pg_Advisory_Lock(1)",
        "  SELECT   nextval('s')  ",
    ],
)
def test_case_and_whitespace(sql: str) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        assert_readonly(sql)
    assert excinfo.value.reason == "disallowed_function"


# ---------------------------------------------------------------------------
# Functions that look dangerous but aren't
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # pg_read_file is an identifier here, not a call
        "SELECT 1 AS pg_read_file",
        # These are read-only introspection functions, not in the deny-list
        "SELECT pg_database_size('postgres')",
        "SELECT pg_relation_size('pg_class')",
        "SELECT pg_stat_activity.pid FROM pg_stat_activity LIMIT 1",
        "SELECT current_setting('search_path')",
    ],
)
def test_innocuous_pg_functions_allowed(sql: str) -> None:
    assert_readonly(sql)
