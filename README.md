# pg-mcp

A **read-only PostgreSQL MCP server** for Claude Code and other
[Model Context Protocol](https://modelcontextprotocol.io) clients.
Point it at one or more Postgres databases and an AI assistant can
safely explore schemas, inspect sample rows, draft queries, and read
`EXPLAIN` plans — without any risk of writing.

Why "safely"? Because read-only is enforced across **three independent
layers**:

1. The Postgres role pg-mcp connects as must have **only `SELECT`**
   grants (validated at startup via a `CREATE TEMP TABLE` probe that
   MUST fail with SQLSTATE `25006`).
2. Every query runs inside `BEGIN; SET TRANSACTION READ ONLY; …;
   ROLLBACK;`.
3. Every SQL string is parsed with `pglast` (the real Postgres C
   parser) and walked by a `Visitor` that rejects any DML, utility
   statement, or deny-listed function anywhere in the AST — including
   smuggled inside CTEs or `EXPLAIN`.

Any single layer failing cannot result in a write.

## Status

Alpha. The safety boundary has 127 unit tests covering ~every known
SQL escape vector (CTE-DML smuggling, `EXPLAIN ANALYZE` on DML,
schema-qualified `pg_catalog.nextval(…)`, `pg_read_file`,
`pg_advisory_lock`, `dblink_exec`, `DO` blocks, `COPY`, multi-statement,
…). End-to-end stdio handshake is tested against the real MCP Python
SDK.

## Install

```bash
# If you have uv:
uv tool install pg-mcp

# Or with pipx:
pipx install pg-mcp

# Or with pip in a venv:
pip install pg-mcp
```

Python 3.11+ is required.

## Quick start (5 minutes)

### 1. Create the read-only Postgres role

On each database you want to expose, run (as a superuser):

```bash
pg-mcp grants myconn --role pg_mcp_ro --password CHANGE_ME
```

That prints a SQL snippet you can paste into `psql`:

```sql
CREATE ROLE pg_mcp_ro LOGIN PASSWORD 'CHANGE_ME';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE mydb TO pg_mcp_ro;
GRANT USAGE ON SCHEMA public TO pg_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_mcp_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO pg_mcp_ro;
ALTER ROLE pg_mcp_ro SET default_transaction_read_only = on;
```

### 2. Create a config

```bash
pg-mcp init
```

Edit `~/.config/pg-mcp/config.yaml` to describe your databases:

```yaml
log_sql: hash   # hash | redacted | full  (see §Observability)

defaults:
  statement_timeout_ms: 30000
  row_limit: 1000
  byte_limit: 1048576
  cell_limit: 8192

connections:
  - name: prod
    description: Production analytics replica (read-only)
    dsn: postgresql://pg_mcp_ro@prod-replica.example.com:5432/appdb?sslmode=require
    password: ${PROD_PG_PASSWORD}
    pool:
      min_size: 1
      max_size: 5

  - name: analytics
    host: warehouse.example.com
    database: analytics
    user: pg_mcp_ro
    password: ${ANALYTICS_PG_PASSWORD}
    sslmode: verify-full
    sslrootcert: ./certs/ca.pem   # relative to config file
```

### 3. Validate

```bash
pg-mcp check
```

Every connection either reports `[OK]` (role grants verified, RO probe
passed) or an actionable error message.

### 4. Register with Claude Code

```bash
claude mcp add --transport stdio pg-mcp -- pg-mcp serve
```

Or add to a project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "pg-mcp": {
      "type": "stdio",
      "command": "pg-mcp",
      "args": ["serve"]
    }
  }
}
```

## Tools

All tools are marked `readOnlyHint=True`, `openWorldHint=False`.

| Tool | Parameters | Purpose |
|---|---|---|
| `list_connections` | — | Show configured DBs + status (available / unavailable / unsafe / pending). Always call first. |
| `list_schemas` | `connection`, `include_system=false` | Schemas visible to the RO role. |
| `list_tables` | `connection`, `schema`, `include_partitions=false`, `limit=500`, `offset=0` | Ordinary + partitioned + foreign tables. Partition children hidden by default. Paginated with `total_count`. |
| `list_views` | `connection`, `schema`, `limit=500`, `offset=0` | Views and materialized views. |
| `describe_table` | `connection`, `schema`, `table` | Columns (types, nullable, default, identity, generated, comment), PK, unique/check constraints, FKs, indexes, inheritance, partition key, RLS, row estimate, size. |
| `describe_view` | `connection`, `schema`, `view` | Columns + `pg_get_viewdef`. Flags broken views. |
| `sample_rows` | `connection`, `schema`, `table`, `limit=20` | `SELECT * LIMIT N`. Preamble includes `table_estimated_rows` and `rls_enabled` so you can distinguish empty from RLS-filtered. |
| `run_query` | `connection`, `sql`, `limit` | Execute `SELECT` / `EXPLAIN`. Full safety pipeline. |
| `explain_query` | `connection`, `sql`, `analyze=false` | `EXPLAIN` (or `EXPLAIN ANALYZE` for plain SELECTs). |
| `search_schema` | `connection`, `pattern`, `kind='all'`, `limit=100` | LIKE search across tables, views, columns, functions. |
| `table_stats` | `connection`, `schema`, `table` | Approx rows, size, last vacuum/analyze, live/dead tuples. |

Run `pg-mcp tools` for the full, always-in-sync catalogue.

## Result format

Every tool returns a markdown block preceded by a metadata preamble
in an HTML comment:

```
<!-- pg-mcp result
connection: prod
duration_ms: 42
rows_returned: 27
truncated_rows: false
truncated_bytes: false
notices: []
-->

| id (integer) | email (text)         |
|---|---|
| 1            | alice@example.com    |
| 2            | bob@example.com      |

(27 rows)
```

### Rendering contract

| Postgres type                | Output                                             |
|------------------------------|----------------------------------------------------|
| `NULL`                       | literal `NULL` (uppercase, unquoted)               |
| `text`, `varchar`            | raw text; `\|`, `\n`, NUL escaped for markdown     |
| `bool`                       | `true` / `false`                                   |
| `int*`, `float*`, `numeric`  | `str(value)`                                       |
| `uuid`, `inet`, `cidr`       | `str(value)`                                       |
| `bytea`                      | `\xHHHH…` hex, truncated to 128 hex chars          |
| timestamp / date / time      | ISO 8601                                           |
| arrays (`int[]`, `text[]`)   | Postgres literal `{a,b,c}`                         |
| composite / record           | `(a,b,c)`                                          |
| `jsonb` / `json`             | compact JSON (`separators=(',', ':')`)             |
| Large cells (> `cell_limit`) | truncated with `…(truncated, N chars)` marker      |
| Wide rows (> 20 cols)        | switched to vertical `**row N**` key-value blocks  |

This contract is covered by snapshot tests; any change is a PR that
must be reviewed.

## Safety model

### Layer 1 — Postgres role grants

The connection user must have **only `SELECT`** on the exposed
schemas. At startup, pg-mcp attempts `CREATE TEMP TABLE` inside a
wrapped `READ ONLY` transaction. Postgres must reply with SQLSTATE
`25006` (`read_only_sql_transaction`). If the statement succeeds,
pg-mcp refuses to use the connection and marks it **`unsafe`** — no
query will ever run through it.

### Layer 2 — `READ ONLY` transactions

Every query runs inside:

```sql
BEGIN;
SET LOCAL statement_timeout = <configured>;
SET LOCAL idle_in_transaction_session_timeout = 5000;
SET TRANSACTION READ ONLY;
-- … query …
ROLLBACK;
```

The pool's `configure` hook additionally pins
`default_transaction_read_only = on` on every new backend, so even a
connection used outside `transaction()` is read-only.

### Layer 3 — SQL parser allow-list + function deny-list

Every user SQL is parsed with **pglast** (the real Postgres C parser)
before it reaches Postgres. Only these top-level statement types are
allowed:

- `SelectStmt` (without an `INTO` clause)
- `ExplainStmt`
- `VariableShowStmt` (`SHOW …`)

A `Visitor` walks the **entire AST** and rejects any forbidden node
anywhere, including inside CTEs, subqueries, `EXPLAIN.query`, and set
operations. This catches:

- `WITH x AS (INSERT … RETURNING *) SELECT * FROM x` — DML in CTE
- `EXPLAIN ANALYZE UPDATE …` — ANALYZE executes
- `SELECT 1; DROP TABLE t` — multi-statement

A **function deny-list** catches side-effect-ful calls that *would
parse as legal SELECTs*. The deny-list is checked on the unqualified
name so `pg_catalog.pg_read_file(…)` is blocked too:

```
pg_read_file, pg_read_binary_file, pg_ls_dir, pg_stat_file, pg_ls_logdir,
pg_ls_waldir, lo_export, lo_import, lo_put, lo_get, lo_from_bytea,
dblink, dblink_exec, dblink_send_query, dblink_connect,
pg_advisory_lock, pg_advisory_xact_lock, pg_try_advisory_lock,
pg_advisory_unlock_all, pg_notify, pg_terminate_backend, pg_cancel_backend,
pg_reload_conf, pg_rotate_logfile, pg_logical_emit_message,
set_config, nextval, setval,
pg_create_logical_replication_slot, pg_drop_replication_slot, …
```

(Plus any function whose name starts with `pg_ls_`, `lo_`, or
`dblink`.)

### What this model does NOT catch

- **User-defined functions that write internally.** E.g., a custom
  function that does `INSERT` inside its body. Layer 1 (role) and
  Layer 2 (RO txn) both block these — the parser alone cannot see
  their body. This is the main reason all three layers are required.
- **Functions we haven't added to the deny-list.** Postgres
  extensions (pg_cron, pg_audit, etc.) can add their own. Report
  anything missing as an issue.

## Error codes

Every error surfaced to the LLM has one of these stable codes:

| Code                         | Meaning                                                              |
|------------------------------|----------------------------------------------------------------------|
| `invalid_parameter`          | A tool argument is out of range or the wrong type                    |
| `unknown_connection`         | `connection` name doesn't match any in config                         |
| `connection_unavailable`     | DB unreachable or probe in progress                                   |
| `connection_unsafe`          | Startup RO probe did not get SQLSTATE 25006 — refused                  |
| `connection_pool_exhausted`  | All connections in use; acquire timed out                             |
| `sql_rejected_by_policy`     | Parser layer rejected the SQL; see `reason` sub-code                 |
| `query_timeout`              | `statement_timeout` fired (SQLSTATE 57014)                            |
| `postgres_error`             | Any other Postgres error; includes `sqlstate`                         |
| `config_error`               | Startup-only; fatal                                                  |

Sub-codes for `sql_rejected_by_policy`:

| Sub-code                          | Meaning                                                    |
|-----------------------------------|------------------------------------------------------------|
| `empty_sql`                       | SQL is empty/whitespace/comment-only                        |
| `sql_too_long`                    | Exceeds 100 KB (configurable)                               |
| `sql_parse_error`                 | pglast could not parse                                      |
| `multiple_statements_not_allowed` | More than one statement (e.g., `SELECT 1; DROP TABLE t`)     |
| `disallowed_statement`            | Top-level or nested node not in allow-list (e.g., InsertStmt) |
| `disallowed_function`             | Deny-listed function call (e.g., pg_read_file)               |
| `dml_in_explain_analyze`          | `EXPLAIN ANALYZE` of non-SELECT would execute the DML        |

## Observability

### Audit log

Every tool call writes one JSON line to the audit log. Default path:

- macOS: `~/Library/Logs/pg-mcp/pg-mcp.log`
- Linux: `$XDG_STATE_HOME/pg-mcp/pg-mcp.log` (falls back to
  `~/.local/state/pg-mcp/pg-mcp.log`)

Rotated at 50 MiB × 5 files (gzip on rotation). WARN+ mirrored to
stderr.

Example entry:

```json
{
  "ts": "2026-04-23T10:00:00.123Z",
  "event": "tool_call",
  "request_id": "a1b2c3d4e5f6",
  "tool": "run_query",
  "connection": "prod",
  "params": {"limit": 1000},
  "sql_hash": "sha256:deadbeef12345678",
  "sql_preview": "SELECT id, email FROM users WHERE created_at > '2026-01-01'",
  "duration_ms": 412,
  "rows_returned": 27,
  "truncated_rows": false,
  "truncated_bytes": false,
  "status": "ok",
  "error_code": null,
  "sqlstate": null
}
```

### SQL logging modes

Configured via `log_sql:` in the config file:

| Mode       | What's written                                                   |
|------------|------------------------------------------------------------------|
| `hash`     | SHA-256 hash + 200-char preview. **Default**; PII-safe.         |
| `redacted` | SQL with string/numeric literals replaced by `?`.                |
| `full`     | Full SQL. **Opt-in**; see PII warning.                           |

## CLI

```
pg-mcp serve           # run MCP server over stdio (default)
pg-mcp init            # write a starter config to ~/.config/pg-mcp/config.yaml
pg-mcp check           # validate config + probe all connections
pg-mcp tools           # print the tool catalogue in markdown
pg-mcp grants NAME     # print DDL for creating the RO role
pg-mcp version         # print version info
pg-mcp --config PATH … # override config discovery
```

## Development

```bash
git clone …
cd pg-mcp
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/ruff format src tests
.venv/bin/mypy src/pg_mcp/safety.py src/pg_mcp/config.py src/pg_mcp/errors.py
```

Integration tests require a live Postgres reachable via
`PG_MCP_TEST_DSN`:

```bash
PG_MCP_TEST_DSN=postgresql://postgres@localhost:5432/postgres \
  .venv/bin/pytest tests/integration
```

## License

MIT. See [LICENSE](./LICENSE).
