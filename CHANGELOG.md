# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **New diagnostic subcommand `pg-mcp doctor`** — comprehensive
  environment + config + per-connection check with actionable
  remediation hints. Catches the macOS Rosetta arm64-mismatch gotcha,
  missing grants, unreachable DBs, missing `pg_stat_statements`, etc.
- **New diagnostic subcommand `pg-mcp info NAME`** — detailed view
  of one connection: status, redacted DSN, pool stats, allowed/denied
  schemas, search_path, timeouts.
- **New MCP tool `reconnect(name)`** — close and re-open the pool
  for a named connection without restarting the server.
- **New MCP tool `related_tables(schema, table)`** — walks the FK
  graph and returns every relationship touching a table.
- **New MCP tool `slow_queries(limit, min_mean_ms)`** — top slow
  queries from `pg_stat_statements` with a helpful
  `extension_missing` error when the extension isn't installed.
- **New MCP tool `diff_schemas(schema_a, schema_b)`** — compares two
  schemas in the same DB.
- **`sample_rows` optional `where` parameter** — WHERE clause
  runs through the full safety gate.
- **Per-connection `allowed_schemas` / `denied_schemas` config** —
  applied both as a post-filter to introspection tools and as a
  pre-flight parser check for `run_query` / `explain_query`.
- **Per-connection `rate_limit_per_minute` config** — rolling-window
  limiter protects prod replicas from runaway LLM loops.
- **Pool stats in `list_connections`** — shows open/max connections
  and waiting requests.
- **New error codes:** `rate_limited`, `disallowed_schema`,
  `extension_missing`.

### Fixed
- `_human_bytes` lost precision via floor division —
  `1_500_000_000` now renders as `1.4 GiB`, not `1.0 GiB`.
- `ForeignKeyInfo` was half-populated — `references_table`,
  `columns`, `references_columns`, `on_update`, `on_delete` are now
  all resolved from `pg_constraint` + `pg_attribute`.
- Primary-key column extraction was parsing `pg_get_indexdef` output
  with `rfind("(")` — fragile for expression indexes. Now reads
  `pg_index.indkey` directly.
- `explain_query` now strips a leading `EXPLAIN [(...)]` from the
  user's SQL before wrapping, so `EXPLAIN SELECT 1` no longer
  produces a syntax error.
- Identifier arguments (schema / table / view) are validated up front
  — non-strings, empty strings, >63 chars, and control characters
  fail with a clean `invalid_parameter` error.

### Changed
- **Audit log `sql_preview` is now redacted by default.** In `hash`
  and `redacted` modes string / numeric literals in the preview are
  replaced with `?` so PII in WHERE clauses doesn't leak even
  though `sql_full` is not written. `full` mode preserves literals.
- **Audit log file is `chmod 0600` on creation**.
- **README extended** with a comprehensive error-code catalogue
  (including Postgres SQLSTATE cross-reference) and `doctor` usage
  guidance.
- **CLI `pg-mcp grants` default role name** is now `pgmcp_ro` (was
  `pg_mcp_ro`, which Postgres reserves).

## [0.1.0] - 2026-04-23

Initial alpha release.

### Added
- `FastMCP` stdio server exposing 11 tools: `list_connections`,
  `list_schemas`, `list_tables`, `list_views`, `describe_table`,
  `describe_view`, `sample_rows`, `run_query`, `explain_query`,
  `search_schema`, `table_stats`.
- Three-layer read-only enforcement: Postgres role grants + `READ ONLY`
  transaction + `pglast`-based AST allow-list with deep `Visitor`.
- Function deny-list blocking `pg_read_file`, `pg_advisory_lock`,
  `nextval`, `dblink_exec`, etc., including schema-qualified variants.
- Startup RO probe per connection: asserts `CREATE TEMP TABLE` fails
  with SQLSTATE `25006`; otherwise refuses to use the connection.
- YAML config with `${VAR}` / `${VAR:-default}` substitution and
  Pydantic validation; duplicate keys rejected by a strict loader.
- `AsyncConnectionPool` per named connection with background pool
  opening so the MCP handshake is never blocked by slow/unreachable DBs.
- Markdown result rendering with metadata preamble, cell-level
  truncation, and vertical format for wide rows.
- Audit log as JSONL with rotation and three SQL-logging modes
  (`hash` / `redacted` / `full`).
- CLI subcommands: `serve`, `init`, `check`, `tools`, `grants`,
  `version`.
- 177 unit tests (127 for the SQL safety gate) and 2 end-to-end stdio
  protocol tests.

[0.1.0]: https://github.com/rakeshpatil/pg-mcp/releases/tag/v0.1.0
