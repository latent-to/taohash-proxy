from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from tests.framework.clock import DeterministicClock


class SimulatedFailure(RuntimeError):
    """Raised by mocks when a deterministic simulated failure is injected."""


DelaySampler = Callable[[random.Random], float]
ExceptionFactory = Callable[[], Exception]


def constant_delay(seconds: float) -> DelaySampler:
    return lambda rng: float(seconds)


def jitter_delay(min_seconds: float, max_seconds: float) -> DelaySampler:
    """Delay sampler returning a deterministic random value between bounds."""

    min_delay = float(min_seconds)
    max_delay = float(max_seconds)

    def _sample(rng: random.Random) -> float:
        return float(rng.uniform(min_delay, max_delay))

    return _sample


@dataclass
class FaultPlan:
    """Composable, deterministic delay/failure injection for mocks.

    The allocator runs across multiple systems (Redis, Postgres, pools).
    This class allows tests to model slow or failing dependencies deterministically.

    Usage:
        faults = (
            FaultPlan(seed=1337)
            .with_latency("redis.publish", seconds=0.05)
            .fail_times("db.execute", times=1)
        )

    Mocks call `await faults.before("operation.name", clock)` before performing an operation.
    """

    seed: int = 0

    # Per-operation delay sampler - if present, it will be awaited before the operation.
    delays: dict[str, DelaySampler] = field(default_factory=dict)

    # Per-operation failures.
    fail_next: dict[str, int] = field(default_factory=dict)
    exception_factories: dict[str, ExceptionFactory] = field(default_factory=dict)

    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(int(self.seed))

    def with_latency(self, operation: str, *, seconds: float) -> "FaultPlan":
        """Apply a constant latency to an operation."""
        self.delays[str(operation)] = constant_delay(float(seconds))
        return self

    def with_jitter(
        self, operation: str, *, min_seconds: float, max_seconds: float
    ) -> "FaultPlan":
        """Apply a deterministic random latency to an operation."""
        self.delays[str(operation)] = jitter_delay(min_seconds, max_seconds)
        return self

    def fail_times(
        self, operation: str, *, times: int = 1, exception: Optional[Exception] = None
    ) -> "FaultPlan":
        """Fail the next `times` calls to operation.

        If `exception` is provided, that exception will be raised; otherwise
        `SimulatedFailure` is raised.
        """
        operation = str(operation)
        self.fail_next[operation] = self.fail_next.get(operation, 0) + int(times)

        if exception is not None:

            def _factory(captured_exception=exception) -> Exception:
                return captured_exception

            self.exception_factories[operation] = _factory
        return self

    async def before(self, operation: str, clock: DeterministicClock) -> None:
        """Apply injected delay/failure for a given operation."""
        operation = str(operation)

        delay_sampler = self.delays.get(operation)
        if delay_sampler is not None:
            await clock.sleep(float(delay_sampler(self._rng)))

        remaining_failures = int(self.fail_next.get(operation, 0) or 0)
        if remaining_failures <= 0:
            return

        self.fail_next[operation] = remaining_failures - 1

        exception_factory = self.exception_factories.get(operation)
        if exception_factory is None:
            raise SimulatedFailure(f"Simulated failure for operation={operation}")
        raise exception_factory()
