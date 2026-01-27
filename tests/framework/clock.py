from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class DeterministicClock:
    """A deterministic clock with time control.

    The allocator is time-sensitive (cooldowns, EMA smoothing, warmup windows,
    and exponential backoff). In unit/integration tests we want to:

      * freeze "now" for repeatable snapshots
      * advance time without sleeping
      * use the same clock for mocks (Redis TTLs, network latency)

    This clock always returns timezone-aware UTC datetimes.
    """

    _current_time: datetime

    @classmethod
    def start_at(cls, start: Optional[datetime] = None) -> "DeterministicClock":
        """Create a clock starting at `start`."""
        if start is None:
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return cls(_current_time=start)

    def now(self) -> datetime:
        """Current time (UTC, tz-aware)."""
        if self._current_time.tzinfo is None:
            return self._current_time.replace(tzinfo=timezone.utc)
        return self._current_time

    def unix(self) -> float:
        """Current Unix timestamp (seconds, UTC)."""
        return float(self.now().timestamp())

    def freeze(self, at: datetime) -> "DeterministicClock":
        """Set the current time to `at` (UTC)."""
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        self._current_time = at
        return self

    def advance(
        self, *, seconds: float = 0.0, minutes: float = 0.0, hours: float = 0.0
    ) -> "DeterministicClock":
        """Advance time."""
        delta = timedelta(
            seconds=float(seconds), minutes=float(minutes), hours=float(hours)
        )
        self._current_time = self.now() + delta
        return self

    async def sleep(self, seconds: float) -> None:
        """Async-friendly sleep that advances time."""
        self.advance(seconds=float(seconds))

    def __repr__(self) -> str:  # pragma: no cover
        return f"DeterministicClock(now={self.now().isoformat()})"
