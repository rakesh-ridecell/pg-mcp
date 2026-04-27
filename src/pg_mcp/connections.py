"""Connection registry: one AsyncConnectionPool per configured database.

The registry is the sole owner of pool lifecycle. Tools ask it for a
pool by name; the pool's ``configure`` hook pins
``default_transaction_read_only = on`` and sets a per-process
``application_name`` (``pg-mcp/<pid>``) for every new backend so
multiple parallel pg-mcp processes (e.g., one per OpenCode session)
can identify their own backends without stepping on each other's.
"""

from __future__ import annotations

import asyncio
import logging
import os
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


# Unique per-process tag we set as Postgres ``application_name`` on every
# pooled backend. Made unique by PID so two parallel pg-mcp processes
# (e.g., one per OpenCode/Claude Code session) can find their own
# backends in pg_stat_activity without affecting each other's. The
# ``pg-mcp/`` prefix lets operators see at a glance which backends are
# from this tool when running ``SELECT * FROM pg_stat_activity``.
APPLICATION_NAME = f"pg-mcp/{os.getpid()}"


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


async def _cancel_backends(entry: ConnectionEntry) -> int:
    """Cancel every pg-mcp backend currently running a query.

    Opens a short-lived side connection (bypassing the pool so we're
    not contending with the very connections we're trying to cancel)
    and runs ``pg_cancel_backend(pid)`` on each active query whose
    ``application_name`` is *this process's* unique tag.

    Critically we match on ``application_name = 'pg-mcp/<our_pid>'``,
    not on a ``LIKE 'pg-mcp/%'``. This means two parallel pg-mcp
    processes (e.g., one per OpenCode/Claude Code session) cannot
    cancel each other's queries — a ``reconnect`` in one session no
    longer interrupts running queries in any other session.

    Returns the number of backends targeted. Errors are caller's
    responsibility — we don't want to swallow them silently *here*
    because the caller may want to log them.
    """
    conninfo = build_conninfo(entry.config)
    async with (
        await psycopg.AsyncConnection.connect(conninfo, autocommit=True, connect_timeout=3) as side,
        side.cursor() as cur,
    ):
        # Find THIS process's own active backends on this DB.
        await cur.execute(
            """
                SELECT pid FROM pg_stat_activity
                WHERE application_name = %s
                  AND state = 'active'
                  AND pid <> pg_backend_pid()
                """,
            (APPLICATION_NAME,),
        )
        pids = [int(r[0]) for r in await cur.fetchall()]
        cancelled = 0
        for pid in pids:
            try:
                await cur.execute("SELECT pg_cancel_backend(%s)", (pid,))
                cancelled += 1
            except Exception as e:
                logger.warning(
                    "pg_cancel_backend(%s) failed for %s: %s",
                    pid,
                    entry.config.name,
                    e,
                )
    return cancelled


async def _configure_conn(conn: AsyncConnection) -> None:
    """Pool configure hook: belt-and-braces RO + app_name for every backend.

    The pool requires the connection to be returned in IDLE (not
    INTRANS) state, so we commit after the SETs. Since SET is not
    transactional, committing here is effectively a no-op that just
    ends the implicit transaction.
    """
    # SET is a utility statement and doesn't accept bind parameters, so we
    # use literal SQL. The app_name is hard-coded and safe.
    async with conn.cursor() as cur:
        await cur.execute("SET default_transaction_read_only = on")
        # Use the per-process unique tag so two pg-mcp processes don't
        # collide (see APPLICATION_NAME docstring).
        await cur.execute(f"SET application_name = '{APPLICATION_NAME}'")
    await conn.commit()


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

    async def reopen(
        self,
        name: str,
        *,
        timeout: float = 10.0,
    ) -> ConnectionEntry:
        """Close the existing pool (if any) for *name* and open a fresh one.

        Before closing we cancel any in-flight pg-mcp backends so
        ``pool.close()`` doesn't block waiting for a stuck query. The
        close itself also has a short timeout as a second line of
        defense. Blocks until the re-probe has completed.
        """
        entry = self.get_entry(name)
        await self._close_one(entry, timeout=3.0, cancel_in_flight=True)
        await self._open_one(entry, probe=True, timeout=timeout)
        return entry

    async def cancel_in_flight(self, name: str) -> int:
        """Cancel every pg-mcp backend currently running a query on *name*.

        Returns the number of backends signalled. Does not tear down
        the pool — existing idle connections remain usable.
        """
        entry = self.get_entry(name)
        return await _cancel_backends(entry)

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

    async def _close_one(
        self,
        entry: ConnectionEntry,
        *,
        timeout: float = 5.0,
        cancel_in_flight: bool = False,
    ) -> None:
        async with entry._lock:
            if entry.pool is not None:
                if cancel_in_flight:
                    # Best-effort: before closing the pool, cancel any
                    # backends pg-mcp has running on this DB. Uses a
                    # fresh side-connection with a short timeout so it
                    # can't itself hang the shutdown. Skips silently on
                    # any error — the main pool.close() will still run.
                    try:
                        await _cancel_backends(entry)
                    except Exception as e:
                        logger.warning(
                            "cancel_backends failed for %s: %s",
                            entry.config.name,
                            e,
                        )
                try:
                    await entry.pool.close(timeout=timeout)
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

    async def await_pool(self, name: str, *, timeout: float = 10.0) -> AsyncConnectionPool:
        """Return a usable pool, waiting up to *timeout* for PENDING to
        transition to AVAILABLE.

        This is the async variant tools should use — when the server
        has just started, pools may still be opening in the background,
        and we'd rather briefly wait than immediately fail.
        """
        entry = self.get_entry(name)
        if entry.status == ConnectionStatus.UNSAFE:
            raise ConnectionUnsafeError(f"connection {name!r} is marked UNSAFE: {entry.last_error}")
        if entry.status == ConnectionStatus.AVAILABLE and entry.pool is not None:
            return entry.pool

        # Wait for PENDING to transition. Poll at 50ms granularity.
        if entry.status == ConnectionStatus.PENDING:
            import time as _time

            deadline = _time.monotonic() + timeout
            while entry.status == ConnectionStatus.PENDING and _time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if entry.status == ConnectionStatus.AVAILABLE and entry.pool is not None:
                return entry.pool
            if entry.status == ConnectionStatus.UNSAFE:
                raise ConnectionUnsafeError(
                    f"connection {name!r} is marked UNSAFE: {entry.last_error}"
                )

        raise ConnectionUnavailableError(
            f"connection {name!r} is {entry.status.value}: {entry.last_error or 'unknown'}"
        )


__all__ = [
    "ConnectionEntry",
    "ConnectionRegistry",
    "ConnectionStatus",
    "build_conninfo",
    "redact_conninfo",
]
