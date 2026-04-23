# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
