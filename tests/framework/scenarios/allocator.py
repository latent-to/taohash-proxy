from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from src.api.services.market_order_queries import get_orders, update_order_status
from src.marketplace_allocator.executor import ExecutionResult, PlanExecutor
from src.marketplace_allocator.planner import plan_tick
from src.marketplace_allocator.pool_health import (
    PoolHealthDecision,
    PoolHealthRecord,
    PoolHealthState,
    evaluate_pool_health,
)
from src.marketplace_allocator.rules import PlanningInput, default_rules
from src.marketplace_allocator.runtime_state import (
    RuntimeStateSnapshot,
    RuntimeStateStore,
)
from src.marketplace_allocator.types import (
    AllocationPlan,
    AllocationSnapshot,
    AllocatorConfig,
    MoveReason,
)
from tests.framework.assertions import assert_equal
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.clock import DeterministicClock
from tests.framework.mocks.postgres import InMemoryMarketplaceDB
from tests.framework.mocks.redis import InMemoryRedis, RedisClientSpy, RedisPublisherSpy


@dataclass
class AllocatorTickResult:
    now: datetime
    snapshot: AllocationSnapshot
    runtime_view: RuntimeStateSnapshot
    pool_health: Optional[PoolHealthDecision]
    plan: AllocationPlan
    diagnostics: Any
    execution: ExecutionResult


class AllocatorScenario:
    """Run one allocator tick end-to-end using in-memory infrastructure.

    We wire the real planner + executor with
    in-memory doubles that behave like the production interfaces.

    A tick follows the sequence:
      - load active orders
      - snapshot miners
      - load runtime state
      - evaluate pool health
      - run rules + planner
      - execute the plan (DB + pub/sub + runtime updates)
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

        # External systems
        self.redis = InMemoryRedis(clock=self.clock)
        self.redis_client = RedisClientSpy(self.redis)
        self.redis_publisher = RedisPublisherSpy(self.redis)
        self.db = InMemoryMarketplaceDB(clock=self.clock)

        # Allocator components
        self.runtime_state = RuntimeStateStore(self.redis_client)  # type: ignore[arg-type]
        self.executor = PlanExecutor(self.db, self.redis_publisher, self.runtime_state)

        # Mutable miner set
        self.miners: dict[str, SimulatedMiner] = {}

        # Cached last result
        self.last_result: Optional[AllocatorTickResult] = None

        self._applied_pubsub_index: int = 0

    def given_order(
        self,
        order_id: str,
        *,
        price_btc_eh_day: Decimal,
        amount_btc: Decimal,
        balance_btc: Optional[Decimal] = None,
        limit_eh_s: Optional[Decimal] = None,
        status: str = "active",
        host: str = "pool.example",
        port: int = 3333,
        user: str = "user",
        password: str = "x",
        queue_position_time: Optional[datetime] = None,
    ) -> "AllocatorScenario":
        self.db.upsert_order(
            order_id=str(order_id),
            host=host,
            port=port,
            username=user,
            password=password,
            price_btc_eh_day=price_btc_eh_day,
            amount_btc=amount_btc,
            balance_btc=balance_btc,
            limit_eh_s=limit_eh_s,
            status=status,
            queue_position_time=queue_position_time,
        )
        return self

    def given_miner(
        self,
        miner_id: str,
        *,
        hashrate_eh_s: float,
        pool_label: str = "normal",
    ) -> "AllocatorScenario":
        self.miners[miner_id] = SimulatedMiner(
            miner_id=str(miner_id),
            hashrate_eh_s=float(hashrate_eh_s),
            pool_label=str(pool_label),
        )
        return self

    def given_miner_object(self, miner: SimulatedMiner) -> "AllocatorScenario":
        self.miners[miner.miner_id] = miner
        return self

    def given_order_health(
        self, order_id: str, record: PoolHealthRecord
    ) -> "AllocatorScenario":
        async def _persist() -> None:
            await self.runtime_state.persist_order_health({str(order_id): record})

        asyncio.run(_persist())
        return self

    def given_miner_last_moved(
        self, miner_id: str, moved_at: datetime
    ) -> "AllocatorScenario":
        async def _persist() -> None:
            await self.runtime_state.record_miner_moves([str(miner_id)], moved_at)

        asyncio.run(_persist())
        return self

    def miner(self, miner_id: str) -> SimulatedMiner:
        try:
            return self.miners[str(miner_id)]
        except KeyError:
            raise KeyError(
                f"Miner {miner_id!r} not found. Known miners={sorted(self.miners.keys())}"
            )
