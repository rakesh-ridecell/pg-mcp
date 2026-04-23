"""``pg-mcp doctor`` — diagnose common misconfigurations.

This is the "why isn't it working?" subcommand. It runs a battery of
checks against the environment, config, and each configured
connection, and prints a readable report with actionable remediation
tips for each issue.

Exit codes:
    0 — all checks passed
    1 — warnings only
    2 — errors (at least one check failed)
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import psycopg

from pg_mcp.config import Config, ConnectionConfig
from pg_mcp.connections import build_conninfo, redact_conninfo

Severity = Literal["ok", "warn", "error"]


@dataclass
class Check:
    name: str
    severity: Severity = "ok"
    message: str = ""
    remedy: str | None = None


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def worst(self) -> Severity:
        if any(c.severity == "error" for c in self.checks):
            return "error"
        if any(c.severity == "warn" for c in self.checks):
            return "warn"
        return "ok"

    def print(self) -> None:
        icons = {"ok": "[OK]    ", "warn": "[WARN]  ", "error": "[ERROR] "}
        for c in self.checks:
            sys.stderr.write(icons[c.severity] + c.name + "\n")
            if c.message:
                for line in c.message.splitlines():
                    sys.stderr.write(f"         {line}\n")
            if c.remedy:
                sys.stderr.write(f"         → {c.remedy}\n")
        total = len(self.checks)
        errors = sum(1 for c in self.checks if c.severity == "error")
        warnings = sum(1 for c in self.checks if c.severity == "warn")
        sys.stderr.write(
            f"\n{total} checks — {total - errors - warnings} ok, {warnings} warn, {errors} error\n"
        )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_python() -> Check:
    impl = platform.python_implementation()
    ver = platform.python_version()
    # We pin requires-python>=3.11 in pyproject.toml so we can only get
    # here on 3.11+. If a future-you drops that floor, change this.
    return Check(name="Python version", message=f"{impl} {ver}")


def _check_architecture() -> Check:
    """Detect the macOS arm64-vs-universal-binary-loaded-as-x86_64 gotcha.

    MCP clients that run under Rosetta (Claude Code's Bun today, ~4/2026)
    spawn Python as x86_64 when it's a universal binary, which then
    can't load arm64-only wheels like pydantic_core.
    """
    machine = platform.machine()
    if sys.platform != "darwin":
        return Check(name="Architecture", message=f"{sys.platform} {machine}")
    # Detect the architecture Python itself is running as.
    pointer_bits = struct.calcsize("P") * 8
    running_as = "arm64" if machine == "arm64" else "x86_64"
    # Check if the Python binary is a universal / fat executable.
    executable = Path(sys.executable).resolve()
    try:
        out = subprocess.check_output(["file", str(executable)], text=True)
    except Exception:
        return Check(
            name="Architecture",
            message=f"running as {running_as} ({pointer_bits}-bit)",
        )
    if "universal binary" in out.lower() or "2 architectures" in out:
        return Check(
            name="Architecture",
            severity="warn",
            message=(
                f"{executable} is a universal binary currently running as {running_as}. "
                "If an MCP client runs under Rosetta/x86_64 (e.g. Claude Code's Bun "
                "in some versions), it will spawn this Python as x86_64 and fail to "
                "load arm64-only wheels."
            ),
            remedy=(
                "Rebuild the venv with an arch-specific Python: "
                "`rm -rf .venv && /opt/homebrew/bin/python3.12 -m venv .venv && "
                ".venv/bin/pip install -e '.[dev]'`"
            ),
        )
    return Check(name="Architecture", message=f"{machine} (single-arch binary)")


def _check_dependencies() -> Check:
    """Verify all runtime deps import cleanly."""
    missing: list[str] = []
    for pkg in ("mcp", "psycopg", "pglast", "pydantic", "yaml"):
        try:
            __import__(pkg)
        except ImportError as e:
            missing.append(f"{pkg} ({e})")
    if missing:
        return Check(
            name="Dependencies",
            severity="error",
            message="Missing: " + ", ".join(missing),
            remedy="Reinstall dev deps: `.venv/bin/pip install -e '.[dev]'`",
        )
    return Check(name="Dependencies", message="mcp, psycopg, pglast, pydantic, yaml all present")


def _check_pg_mcp_binary() -> Check:
    exe = shutil.which("pg-mcp")
    if not exe:
        return Check(
            name="pg-mcp on PATH",
            severity="warn",
            message="`pg-mcp` is not on PATH — you'll need to reference the full venv path",
            remedy=(
                "Register with the full path:\n"
                "  claude mcp add --transport stdio pg-mcp -- "
                "/Users/you/pg-mcp/.venv/bin/pg-mcp serve"
            ),
        )
    return Check(name="pg-mcp on PATH", message=exe)


def _check_config(cfg: Config, cfg_path: Path) -> Check:
    return Check(
        name="Config file",
        message=(
            f"Loaded {len(cfg.connections)} connection(s) from {cfg_path}\n"
            f"log_sql={cfg.log_sql}, "
            f"defaults.row_limit={cfg.defaults.row_limit}, "
            f"defaults.statement_timeout_ms={cfg.defaults.statement_timeout_ms}"
        ),
    )


async def _check_connection(conn_cfg: ConnectionConfig) -> list[Check]:
    """Run a short battery of checks against one configured connection."""
    checks: list[Check] = []
    conninfo = build_conninfo(conn_cfg)

    # --- 1. TCP reach + authenticate
    try:
        async with await psycopg.AsyncConnection.connect(
            conninfo, autocommit=True, connect_timeout=5
        ) as conn:
            checks.append(
                Check(
                    name=f"{conn_cfg.name}: reachable",
                    message=f"connected as {await _current_user(conn)} ({redact_conninfo(conninfo)})",
                )
            )

            # --- 2. default_transaction_read_only at role/session level
            async with conn.cursor() as cur:
                await cur.execute("SHOW default_transaction_read_only")
                row = await cur.fetchone()
                if row and row[0] == "on":
                    checks.append(
                        Check(
                            name=f"{conn_cfg.name}: default_transaction_read_only",
                            message="on (good — belt-and-braces)",
                        )
                    )
                else:
                    checks.append(
                        Check(
                            name=f"{conn_cfg.name}: default_transaction_read_only",
                            severity="warn",
                            message="off — pg-mcp still enforces READ ONLY per txn, but pinning it on the role is safer",
                            remedy=f"ALTER ROLE {await _current_user(conn)} SET default_transaction_read_only = on;",
                        )
                    )

            # --- 3. RO probe (CREATE TEMP TABLE must fail with 25006)
            try:
                async with conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = 5000")
                    await conn.execute("SET TRANSACTION READ ONLY")
                    await conn.execute(
                        "CREATE TEMP TABLE __pgmcp_doctor_probe (id int) ON COMMIT DROP"
                    )
                # If we get here the CREATE succeeded — connection is UNSAFE.
                checks.append(
                    Check(
                        name=f"{conn_cfg.name}: RO probe",
                        severity="error",
                        message=(
                            "CREATE TEMP TABLE succeeded inside a READ ONLY "
                            "transaction. The role can write; pg-mcp will refuse "
                            "to use it."
                        ),
                        remedy=(f"Have a DBA re-run the DDL from `pg-mcp grants {conn_cfg.name}`."),
                    )
                )
            except psycopg.Error as e:
                sqlstate = getattr(e, "sqlstate", None)
                if sqlstate == "25006":
                    checks.append(
                        Check(
                            name=f"{conn_cfg.name}: RO probe",
                            message="CREATE TEMP TABLE rejected (SQLSTATE 25006)",
                        )
                    )
                else:
                    checks.append(
                        Check(
                            name=f"{conn_cfg.name}: RO probe",
                            severity="warn",
                            message=f"unexpected SQLSTATE {sqlstate}: {e}",
                        )
                    )

            # --- 4. Schema USAGE grants
            checks.append(await _check_schema_grants(conn, conn_cfg))

            # --- 5. SELECT-only grants (try a harmless INSERT on an arbitrary table)
            # Skipped: we'd need a target table, and any INSERT could be
            # noisy. The RO probe already covers the negative case.

            # --- 6. pg_stat_statements availability (for slow_queries)
            checks.append(await _check_pg_stat_statements(conn, conn_cfg))

    except psycopg.OperationalError as e:
        checks.append(_map_operational_error(conn_cfg, e))
    except Exception as e:
        checks.append(
            Check(
                name=f"{conn_cfg.name}: reachable",
                severity="error",
                message=f"{type(e).__name__}: {e}",
            )
        )

    return checks


async def _current_user(conn: psycopg.AsyncConnection) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT current_user")
        row = await cur.fetchone()
        return str(row[0]) if row else "?"


async def _check_schema_grants(conn: psycopg.AsyncConnection, conn_cfg: ConnectionConfig) -> Check:
    """Verify the role has USAGE on at least the allowed schemas (if configured)."""
    target_schemas = conn_cfg.allowed_schemas or []
    if not target_schemas:
        # Count schemas visible to the role as a proxy.
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT count(*) FROM pg_catalog.pg_namespace n
                WHERE pg_catalog.has_schema_privilege(n.oid, 'USAGE')
                  AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND n.nspname NOT IN ('information_schema')
                """
            )
            row = await cur.fetchone()
            count = int(row[0]) if row else 0
        if count == 0:
            return Check(
                name=f"{conn_cfg.name}: schema USAGE",
                severity="error",
                message="Role has USAGE on 0 non-system schemas — tools will find nothing useful",
                remedy=(
                    "Grant USAGE on the schemas you want to expose:\n"
                    "  GRANT USAGE ON SCHEMA <schema> TO <role>;\n"
                    "  GRANT SELECT ON ALL TABLES IN SCHEMA <schema> TO <role>;"
                ),
            )
        return Check(
            name=f"{conn_cfg.name}: schema USAGE",
            message=f"Role sees {count} non-system schema(s)",
        )

    missing: list[str] = []
    async with conn.cursor() as cur:
        for schema in target_schemas:
            await cur.execute("SELECT pg_catalog.has_schema_privilege(%s, 'USAGE')", (schema,))
            row = await cur.fetchone()
            if not (row and row[0]):
                missing.append(schema)
    if missing:
        return Check(
            name=f"{conn_cfg.name}: allowed_schemas grants",
            severity="error",
            message="Missing USAGE on: " + ", ".join(missing),
            remedy=f"GRANT USAGE ON SCHEMA {', '.join(missing)} TO <role>;",
        )
    return Check(
        name=f"{conn_cfg.name}: allowed_schemas grants",
        message=f"All {len(target_schemas)} allowed schema(s) visible",
    )


async def _check_pg_stat_statements(
    conn: psycopg.AsyncConnection, conn_cfg: ConnectionConfig
) -> Check:
    """Report whether pg_stat_statements is installed (for slow_queries tool)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
        installed = (await cur.fetchone()) is not None
    if installed:
        return Check(
            name=f"{conn_cfg.name}: pg_stat_statements",
            message="installed — slow_queries tool will work",
        )
    return Check(
        name=f"{conn_cfg.name}: pg_stat_statements",
        severity="warn",
        message="not installed — slow_queries tool will return 'extension_missing'",
        remedy="CREATE EXTENSION pg_stat_statements;  -- requires superuser + shared_preload_libraries",
    )


_OPERATIONAL_ERROR_MAP = [
    (
        "role",
        "does not exist",
        "The Postgres role in the DSN doesn't exist. "
        "Run `pg-mcp grants <connection>` and paste the DDL into psql.",
    ),
    (
        "password authentication failed",
        "",
        "Wrong password. Check the env var / .pgpass / secret manager.",
    ),
    (
        "no pg_hba.conf entry",
        "",
        "Postgres's pg_hba.conf is blocking this client. "
        "Your DBA needs to add an entry for this host / user / SSL mode.",
    ),
    (
        "SSL",
        "",
        "SSL / TLS issue. Check `sslmode` (should be `require` or `verify-full` for prod) "
        "and `sslrootcert` path.",
    ),
    (
        "Connection refused",
        "",
        "Nothing listening on that host:port. VPN? Bastion? Wrong port? "
        "Try `psql <DSN>` from the same shell to verify network reach.",
    ),
    (
        "could not translate host name",
        "",
        "DNS lookup failed. Check hostname typo / VPN / /etc/hosts.",
    ),
    (
        "timeout",
        "",
        "Connection timed out. Network slowness, missing VPN route, or wrong host.",
    ),
]


def _map_operational_error(conn_cfg: ConnectionConfig, err: Exception) -> Check:
    msg = str(err)
    for needle_a, needle_b, remedy in _OPERATIONAL_ERROR_MAP:
        if needle_a in msg and (not needle_b or needle_b in msg):
            return Check(
                name=f"{conn_cfg.name}: reachable",
                severity="error",
                message=msg,
                remedy=remedy,
            )
    return Check(
        name=f"{conn_cfg.name}: reachable",
        severity="error",
        message=msg,
    )


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------


async def run_doctor(cfg: Config | None, cfg_path: Path | None) -> DoctorReport:
    report = DoctorReport()
    report.add(_check_python())
    report.add(_check_architecture())
    report.add(_check_dependencies())
    report.add(_check_pg_mcp_binary())
    if cfg is None:
        report.add(
            Check(
                name="Config file",
                severity="error",
                message="No config loaded — cannot probe connections",
                remedy="Run `pg-mcp init` then re-run `pg-mcp doctor`",
            )
        )
        return report
    assert cfg_path is not None
    report.add(_check_config(cfg, cfg_path))

    # Probe each connection in parallel.
    connection_checks = await asyncio.gather(*(_check_connection(c) for c in cfg.connections))
    for chunk in connection_checks:
        for c in chunk:
            report.add(c)
    return report


__all__ = ["Check", "DoctorReport", "run_doctor"]
