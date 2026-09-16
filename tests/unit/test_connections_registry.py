"""Tests for ConnectionRegistry.await_pool's automatic UNAVAILABLE retry.

A connection that fails its one-shot startup open latches to UNAVAILABLE
forever unless something re-opens it. These tests exercise the cooldown-
gated retry in `await_pool` without touching a real Postgres — `_open_one`
is monkeypatched to simulate success/failure while leaving `_close_one`
(a no-op when `entry.pool is None`) to run for real.
"""

from __future__ import annotations

import time

import pytest

from pg_mcp.config import ConnectionConfig
from pg_mcp.connections import (
    AUTO_RETRY_COOLDOWN_S,
    ConnectionRegistry,
    ConnectionStatus,
)
from pg_mcp.errors import ConnectionUnavailableError


def _unavailable_registry(monkeypatch, *, reopen_succeeds: bool) -> tuple[ConnectionRegistry, list[int]]:
    cfg = ConnectionConfig(name="db1", host="localhost", port=5432, database="x", user="x")
    registry = ConnectionRegistry([cfg])
    calls: list[int] = []

    async def fake_open_one(self, entry, *, probe, timeout):
        calls.append(1)
        entry.last_open_attempt = time.monotonic()
        if reopen_succeeds:
            entry.status = ConnectionStatus.AVAILABLE
            entry.pool = object()
            entry.last_error = None
        else:
            entry.status = ConnectionStatus.UNAVAILABLE
            entry.pool = None
            entry.last_error = "still down"

    monkeypatch.setattr(ConnectionRegistry, "_open_one", fake_open_one)

    entry = registry.get_entry("db1")
    entry.status = ConnectionStatus.UNAVAILABLE
    entry.pool = None
    entry.last_error = "connection refused"
    return registry, calls


async def test_await_pool_does_not_retry_within_cooldown(monkeypatch) -> None:
    registry, calls = _unavailable_registry(monkeypatch, reopen_succeeds=True)
    registry.get_entry("db1").last_open_attempt = time.monotonic()

    with pytest.raises(ConnectionUnavailableError):
        await registry.await_pool("db1", timeout=1.0)
    assert calls == []


async def test_await_pool_retries_after_cooldown_and_recovers(monkeypatch) -> None:
    registry, calls = _unavailable_registry(monkeypatch, reopen_succeeds=True)
    registry.get_entry("db1").last_open_attempt = time.monotonic() - AUTO_RETRY_COOLDOWN_S - 1

    pool = await registry.await_pool("db1", timeout=1.0)

    assert pool is not None
    assert calls == [1]
    assert registry.get_entry("db1").status == ConnectionStatus.AVAILABLE


async def test_await_pool_retry_can_still_fail(monkeypatch) -> None:
    registry, calls = _unavailable_registry(monkeypatch, reopen_succeeds=False)
    registry.get_entry("db1").last_open_attempt = time.monotonic() - AUTO_RETRY_COOLDOWN_S - 1

    with pytest.raises(ConnectionUnavailableError):
        await registry.await_pool("db1", timeout=1.0)
    assert calls == [1]
