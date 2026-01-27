from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.marketplace_allocator.executor import ExecutionResult, PlanExecutor
from src.marketplace_allocator.runtime_state import RuntimeStateStore
from src.marketplace_allocator.types import AllocationPlan, AllocationSnapshot
from tests.framework.assertions import assert_equal
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.builders.order import SimulatedOrder
from tests.framework.clock import DeterministicClock
from tests.framework.mocks.postgres import InMemoryMarketplaceDB
from tests.framework.mocks.redis import InMemoryRedis, RedisClientSpy, RedisPublisherSpy


@dataclass
class ExecutorResult:
    execution: ExecutionResult


class ExecutorScenario:
    """Scenario runner focused on PlanExecutor side effects."""

    def __init__(
        self,
        name: str = "",
        *,
        clock: Optional[DeterministicClock] = None,
    ) -> None:
        self.name = name
        self.clock = clock or DeterministicClock.start_at()

        self.redis = InMemoryRedis(clock=self.clock)
        self.redis_client = RedisClientSpy(self.redis)
        self.redis_publisher = RedisPublisherSpy(self.redis)

        self.db = InMemoryMarketplaceDB(clock=self.clock)
        self.runtime_state = RuntimeStateStore(self.redis_client)  # type: ignore[arg-type]
        self.executor = PlanExecutor(self.db, self.redis_publisher, self.runtime_state)

        self.orders: dict[str, SimulatedOrder] = {}
        self.miners: dict[str, SimulatedMiner] = {}

        self.last_result: Optional[ExecutorResult] = None

    def given_order(self, order: SimulatedOrder) -> "ExecutorScenario":
        self.orders[order.order_id] = order
        self.db.upsert_order(
            order_id=order.order_id,
            host=order.host,
            port=order.port,
            username=order.user,
            password=order.password,
            price_btc_eh_day=order.price_btc_eh_day,
            amount_btc=order.balance_btc,
            balance_btc=order.balance_btc,
            limit_eh_s=order.limit_eh_s,
            status=order.status,
            queue_position_time=order.queue_position_time,
        )
        return self

    def given_miner(self, miner: SimulatedMiner) -> "ExecutorScenario":
        self.miners[miner.miner_id] = miner
        return self

    def at(self, timestamp: datetime) -> "ExecutorScenario":
        self.clock.freeze(timestamp)
        return self

    def apply(self, plan: AllocationPlan) -> ExecutorResult:
        now = self.clock.now()
        snapshot = AllocationSnapshot(
            now=now,
            orders=[o.to_snapshot(now=now) for o in self.orders.values()],
            miners=[m.to_snapshot() for m in self.miners.values()],
        )

        async def _run() -> ExecutionResult:
            return await self.executor.apply(snapshot, plan)

        execution = asyncio.run(_run())
        self.last_result = ExecutorResult(execution=execution)
        return self.last_result

    def expect_execution_counts(self, *, moved: int, failed: int) -> "ExecutorScenario":
        if not self.last_result:
            raise AssertionError("No plan has been applied yet")

        assert_equal(
            {
                "moved": self.last_result.execution.moved,
                "failed": self.last_result.execution.failed,
            },
            {"moved": moved, "failed": failed},
            message=f"[{self.name}] execution counts mismatch",
        )
        return self

    def expect_published_command_types(
        self, expected_types: list[str]
    ) -> "ExecutorScenario":
        actual_types = [
            p.get("type") for p in self.redis.published_json(channel="miner:commands")
        ]
        assert_equal(
            actual_types,
            expected_types,
            message=f"[{self.name}] published command types mismatch",
        )
        return self
