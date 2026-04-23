"""Tests for the per-connection rate limiter."""

from __future__ import annotations

import asyncio

import pytest

from pg_mcp.errors import RateLimitedError
from pg_mcp.ratelimit import RateLimiter


async def test_disabled_when_no_limit() -> None:
    rl = RateLimiter(limit_per_minute=None)
    assert not rl.enabled
    # Unlimited calls must not raise.
    for _ in range(100):
        await rl.check_and_record("x")


async def test_allows_up_to_limit() -> None:
    rl = RateLimiter(limit_per_minute=3, window_s=60)
    for _ in range(3):
        await rl.check_and_record("x")


async def test_rejects_over_limit() -> None:
    rl = RateLimiter(limit_per_minute=3, window_s=60)
    for _ in range(3):
        await rl.check_and_record("x")
    with pytest.raises(RateLimitedError) as excinfo:
        await rl.check_and_record("x")
    assert "3 calls/min" in str(excinfo.value)


async def test_sliding_window_lets_old_calls_age_out() -> None:
    """Events older than window_s drop out on the next check."""
    rl = RateLimiter(limit_per_minute=2, window_s=0.05)  # 50ms window for testing
    await rl.check_and_record("x")
    await rl.check_and_record("x")
    with pytest.raises(RateLimitedError):
        await rl.check_and_record("x")
    # Wait for window to slide
    await asyncio.sleep(0.1)
    # Now we should be able to make more calls
    await rl.check_and_record("x")
    await rl.check_and_record("x")


async def test_concurrent_calls_are_serialized_correctly() -> None:
    rl = RateLimiter(limit_per_minute=5)
    # Fire 10 concurrent requests; exactly 5 should succeed.
    results = await asyncio.gather(
        *(rl.check_and_record(f"c{i}") for i in range(10)),
        return_exceptions=True,
    )
    successes = sum(1 for r in results if r is None)
    failures = sum(1 for r in results if isinstance(r, RateLimitedError))
    assert successes == 5
    assert failures == 5


def test_snapshot_reports_state() -> None:
    rl = RateLimiter(limit_per_minute=10)
    snap = rl.snapshot()
    assert snap["limit_per_minute"] == 10
    assert snap["calls_last_window"] == 0
