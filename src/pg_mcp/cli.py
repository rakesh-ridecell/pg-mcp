"""CLI entry point for pg-mcp.

Subcommands:

- ``serve`` (default) — run the MCP server over stdio.
- ``init``             — write a commented sample config.
- ``check``            — validate config, probe every connection.
- ``tools``            — print the tool catalogue (human-readable).
- ``grants``           — print RO-role DDL for a named connection.
- ``version``          — print package + dependency versions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import NoReturn

from pg_mcp import __version__
from pg_mcp.audit import AuditLogger, platform_default_log_path
from pg_mcp.config import Config, ConnectionConfig, discover_config_path, load_config
from pg_mcp.connections import ConnectionRegistry, ConnectionStatus
from pg_mcp.errors import ConfigError
from pg_mcp.stdio_guard import configure_logging_to_stderr

logger = logging.getLogger(__name__)

SAMPLE_CONFIG = """\
# pg-mcp configuration
# Docs: see README in the pg-mcp repo.
#
# Env-var substitution: ${VAR} or ${VAR:-default}. Bare $VAR is NOT supported.

# Where to write the JSONL audit log. Defaults to a platform-specific path
# if omitted. Set to empty string to disable file logging.
# log_file: ~/Library/Logs/pg-mcp/pg-mcp.log

# How much of each executed SQL to write to the audit log:
#   hash     — sha256 + 200-char preview (default; PII-safe)
#   redacted — literals replaced by '?'
#   full     — full SQL (opt-in; may contain PII)
log_sql: hash

defaults:
  statement_timeout_ms: 30000   # per-query cancellation
  row_limit: 1000               # max rows returned by run_query
  byte_limit: 1048576           # ~1 MiB response cap
  cell_limit: 8192              # truncate individual cells above this

connections:
  - name: example
    description: Example connection — replace with your own
    dsn: postgresql://pg_mcp_ro@localhost:5432/mydb
    password: ${EXAMPLE_PG_PASSWORD:-}
    pool:
      min_size: 1
      max_size: 5
    # Optional per-connection overrides:
    # statement_timeout_ms: 60000
    # row_limit: 500
    # search_path: [app, public]
    # sslmode: require
    # sslrootcert: ./certs/rds-ca.pem     # relative to this file
"""

RO_GRANTS_TEMPLATE = """\
-- Run as a Postgres superuser / DB owner to create the RO role used by pg-mcp.
-- Replace <role>, <password>, <database>, and the schema list as needed.

CREATE ROLE {role} LOGIN PASSWORD '{password}';

-- Never let this role create new objects.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE {database} TO {role};

-- Repeat the USAGE + SELECT grants for each schema you want to expose.
GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role};

-- Pin read-only at the role level too (belt-and-suspenders).
ALTER ROLE {role} SET default_transaction_read_only = on;
"""


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> NoReturn:
    args = _parser().parse_args(argv)
    if args.command is None:
        args.command = "serve"

    if args.command == "version":
        print(f"pg-mcp {__version__}")
        _print_dep_versions()
        sys.exit(0)

    if args.command == "init":
        sys.exit(_cmd_init(args))

    if args.command == "tools":
        sys.exit(_cmd_tools())

    if args.command == "grants":
        sys.exit(_cmd_grants(args))

    # For serve / check we need a config.
    cfg_path = discover_config_path(args.config)
    if cfg_path is None:
        sys.stderr.write(
            "No config found. Searched:\n"
            "  - --config <path>\n"
            "  - PG_MCP_CONFIG env var\n"
            "  - $XDG_CONFIG_HOME/pg-mcp/config.yaml\n"
            "  - ~/.config/pg-mcp/config.yaml\n\n"
            "Run `pg-mcp init` to create a starter config.\n"
        )
        sys.exit(2)

    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        sys.stderr.write(f"config error: {e}\n")
        sys.exit(2)

    sys.stderr.write(f"pg-mcp: loaded config from {cfg_path}\n")

    if args.command == "check":
        sys.exit(_cmd_check(cfg))

    if args.command == "serve":
        sys.exit(_cmd_serve(cfg))

    sys.stderr.write(f"unknown command: {args.command}\n")
    sys.exit(2)


# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-mcp",
        description="Read-only PostgreSQL MCP server.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.yaml (overrides discovery).",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the MCP server over stdio (default).")
    p_init = sub.add_parser("init", help="Write a starter config.yaml.")
    p_init.add_argument(
        "--path",
        type=Path,
        help="Where to write the config (default: ~/.config/pg-mcp/config.yaml).",
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file.",
    )
    sub.add_parser("check", help="Validate config and probe connections.")
    sub.add_parser("tools", help="Print the tool catalogue.")
    p_grants = sub.add_parser(
        "grants",
        help="Print DDL to create a read-only Postgres role.",
    )
    p_grants.add_argument("name", help="Connection name (from config).")
    p_grants.add_argument(
        "--role",
        default="pg_mcp_ro",
        help="Role name to create (default: pg_mcp_ro).",
    )
    p_grants.add_argument(
        "--password",
        default="CHANGE_ME",
        help="Placeholder password in the generated DDL.",
    )
    sub.add_parser("version", help="Print version info.")
    return parser


# ---------------------------------------------------------------------------
# init


def _cmd_init(args: argparse.Namespace) -> int:
    path: Path = args.path or (Path.home() / ".config" / "pg-mcp" / "config.yaml")
    path = path.expanduser()
    if path.exists() and not args.force:
        sys.stderr.write(f"{path} already exists (pass --force to overwrite)\n")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    sys.stderr.write(
        f"Wrote starter config to {path}\nEdit the file, then run `pg-mcp check` to validate.\n"
    )
    return 0


# ---------------------------------------------------------------------------
# tools


def _cmd_tools() -> int:
    try:
        from pg_mcp.server import build_server
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f"cannot load server: {e}\n")
        return 2
    # Build a throwaway config to enumerate tools.
    from pg_mcp.config import Config

    cfg = Config(
        connections=[
            ConnectionConfig(
                name="placeholder",
                dsn="postgresql://u@h/d",
            )
        ]
    )
    audit = AuditLogger(log_file=Path(os.devnull), log_sql="hash")
    server = build_server(cfg, audit)
    # FastMCP stores tools internally; iterate via the public API.
    # In mcp>=1.27, `server._tool_manager.list_tools()` is async; use a
    # sync fallback via the internal map.
    try:
        tool_manager = server._tool_manager  # type: ignore[attr-defined]
        tools = tool_manager._tools  # type: ignore[attr-defined]
    except AttributeError:
        sys.stderr.write(
            "Unable to enumerate tools — MCP SDK internals changed. "
            "Use `claude mcp list` to inspect via the client instead.\n"
        )
        return 1
    print(f"# pg-mcp tools ({len(tools)})\n")
    for name, tool in sorted(tools.items()):
        description = (tool.description or "").strip()
        print(f"## `{name}`")
        print()
        print(description)
        if getattr(tool, "parameters", None):
            print()
            print("**Parameters:**")
            print("```json")
            print(json.dumps(tool.parameters, indent=2, default=str))
            print("```")
        print()
    return 0


# ---------------------------------------------------------------------------
# grants


def _cmd_grants(args: argparse.Namespace) -> int:
    # This command doesn't actually need a live config — the template is static.
    cfg_path = discover_config_path(args.config if hasattr(args, "config") else None)
    database = "<database>"
    if cfg_path:
        try:
            cfg = load_config(cfg_path)
            conn = cfg.get(args.name)
            if conn is not None:
                database = conn.database or "<database>"
                if not conn.database and conn.dsn:
                    try:
                        import psycopg

                        dsn = psycopg.conninfo.conninfo_to_dict(conn.dsn)
                        database = dsn.get("dbname", "<database>")
                    except Exception:  # pragma: no cover
                        pass
        except ConfigError:
            pass
    print(
        RO_GRANTS_TEMPLATE.format(
            role=args.role,
            password=args.password,
            database=database,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# check


def _cmd_check(cfg: Config) -> int:
    configure_logging_to_stderr(level="WARNING")
    registry = ConnectionRegistry(cfg.connections)

    async def _run() -> None:
        await registry.open_all(probe=True)
        for entry in registry.entries():
            icon = {
                ConnectionStatus.AVAILABLE: "[OK]",
                ConnectionStatus.UNAVAILABLE: "[UNAVAILABLE]",
                ConnectionStatus.UNSAFE: "[UNSAFE]",
                ConnectionStatus.PENDING: "[PENDING]",
            }[entry.status]
            sys.stderr.write(
                f"{icon:<16} {entry.config.name}"
                + (f"  — {entry.last_error}" if entry.last_error else "")
                + "\n"
            )
        await registry.close_all()

    asyncio.run(_run())
    unsafe = any(e.status == ConnectionStatus.UNSAFE for e in registry.entries())
    unavailable = any(e.status == ConnectionStatus.UNAVAILABLE for e in registry.entries())
    if unsafe:
        sys.stderr.write(
            "\nRefusing to use UNSAFE connections. Verify the Postgres role has "
            "only SELECT grants and that `default_transaction_read_only` is on.\n"
        )
        return 3
    if unavailable:
        sys.stderr.write(
            "\nSome connections are unavailable. The server will still start; "
            "unavailable connections surface via `list_connections`.\n"
        )
        return 1

    # Friendly final line with the Claude Code registration snippet.
    sys.stderr.write(
        "\nAll good.\n"
        "Register with Claude Code:\n"
        "  claude mcp add --transport stdio pg-mcp -- pg-mcp serve\n"
    )
    return 0


# ---------------------------------------------------------------------------
# serve


def _cmd_serve(cfg: Config) -> int:
    configure_logging_to_stderr(level="INFO")

    from pg_mcp.server import build_server

    log_file = Path(cfg.log_file).expanduser() if cfg.log_file else platform_default_log_path()
    audit = AuditLogger(log_file=log_file, log_sql=cfg.log_sql)
    server = build_server(cfg, audit)

    # Install signal handlers that deliver a clean shutdown via asyncio.
    # FastMCP.run() uses anyio.run internally; SIGINT is natively handled.
    # For SIGTERM we register a handler that cancels the running task.
    def _handle_term(sig, frame):  # type: ignore[no-untyped-def]
        sys.stderr.write(f"pg-mcp: caught signal {sig}, shutting down…\n")
        audit.shutdown(f"caught signal {sig}")
        # Re-raise as KeyboardInterrupt so anyio unwinds normally.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_term)

    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    return 0


# ---------------------------------------------------------------------------
# Helpers


def _print_dep_versions() -> None:
    for pkg in ("mcp", "psycopg", "pglast", "pydantic", "PyYAML"):
        try:
            from importlib.metadata import version

            v = version(pkg)
        except Exception:
            v = "unknown"
        print(f"  {pkg}: {v}")


if __name__ == "__main__":
    main()
