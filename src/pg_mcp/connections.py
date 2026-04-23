"""Connection registry: one AsyncConnectionPool per configured database.

The registry is the sole owner of pool lifecycle. Tools ask it for a
pool by name; the pool's ``configure`` hook pins
``default_transaction_read_only = on`` and sets a recognizable
``application_name`` for every new backend.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pg_mcp.config import ConnectionConfig
from pg_mcp.errors import (
    ConnectionUnavailableError,
    ConnectionUnsafeError,
    UnknownConnectionError,
)

logger = logging.getLogger(__name__)


class ConnectionStatus(StrEnum):
    """Lifecycle states for a configured connection."""

    PENDING = "pending"  # not yet probed
    AVAILABLE = "available"  # probed; RO enforcement verified
    UNAVAILABLE = "unavailable"  # DB unreachable or probe failed transiently
    UNSAFE = "unsafe"  # probe did not reject a write — refused


@dataclass
class ConnectionEntry:
    config: ConnectionConfig
    pool: AsyncConnectionPool | None = None
    status: ConnectionStatus = ConnectionStatus.PENDING
    last_error: str | None = None
    last_probe_at: str | None = None
    version: int = 0  # bumped on each (re)open attempt; useful for diagnostics
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def build_conninfo(cfg: ConnectionConfig) -> str:
    """Build a libpq-style keyword=value conninfo string from config."""
    if cfg.dsn:
        # psycopg accepts a URL + extra kwargs via conninfo_to_dict, but
        # since we also allow a separate password, merge them cleanly.
        merged = psycopg.conninfo.conninfo_to_dict(cfg.dsn)
        if cfg.password and "password" not in merged:
            merged["password"] = cfg.password
        if cfg.sslrootcert and "sslrootcert" not in merged:
            merged["sslrootcert"] = cfg.sslrootcert
        if cfg.sslcert and "sslcert" not in merged:
            merged["sslcert"] = cfg.sslcert
        if cfg.sslkey and "sslkey" not in merged:
            merged["sslkey"] = cfg.sslkey
        if cfg.connect_timeout and "connect_timeout" not in merged:
            merged["connect_timeout"] = str(cfg.connect_timeout)
        return psycopg.conninfo.make_conninfo(**merged)

    kwargs: dict[str, str | int] = {}
    if cfg.host:
        kwargs["host"] = cfg.host
    if cfg.port:
        kwargs["port"] = cfg.port
    if cfg.database:
        kwargs["dbname"] = cfg.database
    if cfg.user:
        kwargs["user"] = cfg.user
    if cfg.password:
        kwargs["password"] = cfg.password
    if cfg.sslmode:
        kwargs["sslmode"] = cfg.sslmode
    if cfg.sslrootcert:
        kwargs["sslrootcert"] = cfg.sslrootcert
    if cfg.sslcert:
        kwargs["sslcert"] = cfg.sslcert
    if cfg.sslkey:
        kwargs["sslkey"] = cfg.sslkey
    if cfg.connect_timeout:
        kwargs["connect_timeout"] = cfg.connect_timeout
    return psycopg.conninfo.make_conninfo(**kwargs)


def redact_conninfo(conninfo: str) -> str:
    """Return *conninfo* with the password stripped for logging."""
    try:
        d = psycopg.conninfo.conninfo_to_dict(conninfo)
    except Exception:
        return "<unparseable>"
    d.pop("password", None)
    return psycopg.conninfo.make_conninfo(**d)


async def _configure_conn(conn: AsyncConnection) -> None:
    """Pool configure hook: belt-and-braces RO + app_name for every backend."""
    async with conn.cursor() as cur:
        await cur.execute("SET default_transaction_read_only = on")
        await cur.execute("SET application_name = %s", ("pg-mcp",))


class ConnectionRegistry:
    """Owns all connection pools. Safe to call concurrently."""

    def __init__(self, configs: list[ConnectionConfig]) -> None:
        self._entries: dict[str, ConnectionEntry] = {
            c.name: ConnectionEntry(config=c) for c in configs
        }

    @property
    def names(self) -> list[str]:
        return list(self._entries)

    def get_entry(self, name: str) -> ConnectionEntry:
        try:
            return self._entries[name]
        except KeyError:
            raise UnknownConnectionError(
                f"connection {name!r} not in config; known: {sorted(self._entries)}"
            ) from None

    def entries(self) -> list[ConnectionEntry]:
        return list(self._entries.values())

    async def open_all(
        self,
        *,
        probe: bool = True,
        timeout: float = 10.0,
    ) -> None:
        """Open pools for every configured connection in parallel.

        Blocks until all pools have either connected (and been probed if
        ``probe=True``) or reported failure. Use this in CLI commands
        where deterministic output matters.
        """
        await asyncio.gather(
            *(
                self._open_one(entry, probe=probe, timeout=timeout)
                for entry in self._entries.values()
            ),
            return_exceptions=True,
        )

    def open_all_background(
        self,
        *,
        probe: bool = True,
        timeout: float = 10.0,
    ) -> list[asyncio.Task]:
        """Kick off pool opens in the background.

        Used by the MCP server so the stdio handshake isn't blocked on
        slow DB connections. Each connection's status flips from
        ``pending`` to ``available`` / ``unavailable`` / ``unsafe``
        asynchronously. ``list_connections`` surfaces the current status.
        Returns the task handles for tests / graceful shutdown.
        """
        loop = asyncio.get_event_loop()
        return [
            loop.create_task(
                self._open_one(entry, probe=probe, timeout=timeout),
                name=f"pg-mcp:open:{entry.config.name}",
            )
            for entry in self._entries.values()
        ]

    async def close_all(self) -> None:
        await asyncio.gather(
            *(self._close_one(entry) for entry in self._entries.values()),
            return_exceptions=True,
        )

    async def _open_one(
        self,
        entry: ConnectionEntry,
        *,
        probe: bool,
        timeout: float,
    ) -> None:
        async with entry._lock:
            entry.version += 1
            conninfo = build_conninfo(entry.config)
            logger.info(
                "opening pool for %s (dsn=%s, pool=%d..%d)",
                entry.config.name,
                redact_conninfo(conninfo),
                entry.config.pool.min_size,
                entry.config.pool.max_size,
            )
            entry.status = ConnectionStatus.PENDING
            try:
                pool = AsyncConnectionPool(
                    conninfo=conninfo,
                    min_size=entry.config.pool.min_size,
                    max_size=entry.config.pool.max_size,
                    kwargs={"autocommit": False},
                    configure=_configure_conn,
                    name=f"pg-mcp:{entry.config.name}",
                    open=False,
                )
                await pool.open(wait=True, timeout=timeout)
                entry.pool = pool
                entry.last_error = None
            except Exception as e:
                entry.status = ConnectionStatus.UNAVAILABLE
                entry.last_error = f"{type(e).__name__}: {e}"
                entry.pool = None
                logger.warning("pool open failed for %s: %s", entry.config.name, entry.last_error)
                return

            if probe:
                # Lazy import to avoid a cycle at module load.
                from pg_mcp.probes import probe_readonly

                try:
                    await probe_readonly(pool)
                    entry.status = ConnectionStatus.AVAILABLE
                except ConnectionUnsafeError as e:
                    entry.status = ConnectionStatus.UNSAFE
                    entry.last_error = str(e)
                    logger.error(
                        "connection %s marked UNSAFE: %s",
                        entry.config.name,
                        entry.last_error,
                    )
                except Exception as e:
                    entry.status = ConnectionStatus.UNAVAILABLE
                    entry.last_error = f"probe failed: {type(e).__name__}: {e}"
                    logger.warning("probe failed for %s: %s", entry.config.name, entry.last_error)
            else:
                # No probe → assume available once the pool is open.
                entry.status = ConnectionStatus.AVAILABLE

    async def _close_one(self, entry: ConnectionEntry) -> None:
        async with entry._lock:
            if entry.pool is not None:
                try:
                    await entry.pool.close()
                except Exception as e:  # pragma: no cover - best effort
                    logger.warning("error closing pool %s: %s", entry.config.name, e)
                entry.pool = None

    def get_pool(self, name: str) -> AsyncConnectionPool:
        """Return a usable pool, or raise a typed error.

        Refuses to hand out UNSAFE pools on principle even if the pool
        object exists — failing closed is the whole point.
        """
        entry = self.get_entry(name)
        if entry.status == ConnectionStatus.UNSAFE:
            raise ConnectionUnsafeError(f"connection {name!r} is marked UNSAFE: {entry.last_error}")
        if entry.pool is None or entry.status != ConnectionStatus.AVAILABLE:
            raise ConnectionUnavailableError(
                f"connection {name!r} is {entry.status.value}: {entry.last_error or 'unknown'}"
            )
        return entry.pool


__all__ = [
    "ConnectionEntry",
    "ConnectionRegistry",
    "ConnectionStatus",
    "build_conninfo",
    "redact_conninfo",
]
