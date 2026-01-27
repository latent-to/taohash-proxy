from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.marketplace_allocator.pool_health import (
    MinerPoolShareStats,
    PoolHealthDecision,
    PoolHealthRecord,
    PoolHealthState,
    evaluate_pool_health,
)
from src.marketplace_allocator.types import AllocatorConfig
from tests.framework.assertions import assert_equal
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.clock import DeterministicClock


@dataclass
class PoolHealthTickResult:
    now: datetime
    decision: PoolHealthDecision


class PoolHealthScenario:
    """Scenario runner for `evaluate_pool_health`.

    Given a set of miners with cumulative share counters,
    run a tick and inspect the resulting per-order health records.
    """

    def __init__(
        self,
        name: str = "",
        *,
        clock: Optional[DeterministicClock] = None,
        config: Optional[AllocatorConfig] = None,
    ) -> None:
        self.name = name
        self.clock = clock or DeterministicClock.start_at()
        self.config = config or AllocatorConfig()

        self.miners: dict[str, SimulatedMiner] = {}
        self.prev_order_health: dict[str, PoolHealthRecord] = {}
        self.prev_miner_share_stats: dict[str, MinerPoolShareStats] = {}

        self.last_result: Optional[PoolHealthTickResult] = None

    def given_miner(self, miner: SimulatedMiner) -> "PoolHealthScenario":
        self.miners[miner.miner_id] = miner
        return self

    def given_order_health(
        self, order_id: str, record: PoolHealthRecord
    ) -> "PoolHealthScenario":
        self.prev_order_health[str(order_id)] = record
        return self

    def given_prev_miner_counter(
        self, miner_id: str, stats: MinerPoolShareStats
    ) -> "PoolHealthScenario":
        self.prev_miner_share_stats[str(miner_id)] = stats
        return self

    def given_prev_miner_share_stats(
        self, miner_id: str, stats: MinerPoolShareStats
    ) -> "PoolHealthScenario":
        """Seed the previous tick's miner share counters for a miner."""
        self.prev_miner_share_stats[str(miner_id)] = stats
        return self

    def at(self, now: datetime) -> "PoolHealthScenario":
        self.clock.freeze(now)
        return self

    def advance(self, *, seconds: float) -> "PoolHealthScenario":
        self.clock.advance(seconds=float(seconds))
        return self

    def tick(self) -> PoolHealthTickResult:
        now = self.clock.now()
        decision = evaluate_pool_health(
            now=now,
            miners=[m.to_snapshot() for m in self.miners.values()],
            prev_order_health=self.prev_order_health,
            prev_miner_share_stats=self.prev_miner_share_stats,
            config=self.config,
        )

        # Persist for next tick
        self.prev_order_health = decision.order_health
        self.prev_miner_share_stats = decision.miner_share_stats

        self.last_result = PoolHealthTickResult(now=now, decision=decision)
        return self.last_result

    def expect_order_state(
        self, order_id: str, state: PoolHealthState
    ) -> "PoolHealthScenario":
        if not self.last_result:
            raise AssertionError("No tick has been executed yet")

        health_record = self.last_result.decision.order_health.get(str(order_id))
        if health_record is None:
            raise AssertionError(
                f"Order {order_id!r} missing from decision.order_health. "
                f"Known orders={sorted(self.last_result.decision.order_health.keys())}"
            )

        message = f"[{self.name}] expected order {order_id} state {state}, \
                got {health_record.status} (reason={health_record.reason!r})"
        assert_equal(
            health_record.status,
            state,
            message=message,
        )
        return self

    def expect_backoff_until_after_now(self, order_id: str) -> "PoolHealthScenario":
        if not self.last_result:
            raise AssertionError("No tick has been run yet")
        health_record = self.last_result.decision.order_health.get(str(order_id))
        if not health_record:
            raise AssertionError(f"Order {order_id!r} missing from order_health")

        current_timestamp = float(self.last_result.now.timestamp())
        if not (
            health_record.backoff_until_ts
            and health_record.backoff_until_ts > current_timestamp
        ):
            raise AssertionError(
                f"[{self.name}] expected order {order_id} to be in backoff (until>{current_timestamp}) "
                f"but backoff_until_ts={health_record.backoff_until_ts}"
            )
        return self
