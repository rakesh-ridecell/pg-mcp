"""Simple per-connection rate limiting.

A rolling-window counter: we keep a deque of request timestamps and,
on each request, drop entries older than ``window_s`` and check the
remaining count against ``limit``. This avoids the bursty-fixed-window
problem where a client can make 2x the limit by straddling window
edges.

Intentionally in-memory / per-process. For a multi-process deployment
you'd want a shared store, but pg-mcp is single-process per MCP
client.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from pg_mcp.errors import RateLimitedError


class RateLimiter:
    """Rolling-window rate limiter for one connection.

    Thread-safe via an asyncio lock — tools may run concurrently on a
    single FastMCP server.
    """

    def __init__(self, *, limit_per_minute: int | None, window_s: float = 60.0) -> None:
        self.limit = limit_per_minute
        self.window_s = window_s
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit is not None and self.limit > 0

    async def check_and_record(self, name: str) -> None:
        """Record a new request. Raise ``RateLimitedError`` if over cap."""
        if not self.enabled:
            return
        assert self.limit is not None  # mypy
        now = time.monotonic()
        cutoff = now - self.window_s
        async with self._lock:
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            if len(self._events) >= self.limit:
                # Most recently-expiring event tells us how long to wait.
                retry_after = max(0.0, self._events[0] + self.window_s - now)
                raise RateLimitedError(
                    f"connection {name!r} exceeded {self.limit} calls/min "
                    f"(retry in ~{retry_after:.1f}s)"
                )
            self._events.append(now)

    def snapshot(self) -> dict[str, object]:
        """Diagnostic: current count + configured limit."""
        return {
            "limit_per_minute": self.limit,
            "calls_last_window": len(self._events),
        }


__all__ = ["RateLimiter"]
