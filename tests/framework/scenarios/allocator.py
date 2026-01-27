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

    def apply_new_pubsub_commands(
        self, *, default_label: Optional[str] = None
    ) -> "AllocatorScenario":
        """Apply newly published switch commands to the simulated miners.

        Args:
            default_label: Label to use when applying a "switch_default" command.
                If omitted, uses the smallest configured
                default label.
        """
        if default_label is None:
            default_label = sorted(self.config.default_labels)[0]

        commands = self.redis.published_json(channel="miner:commands")
        new_commands = commands[self._applied_pubsub_index :]

        for command in new_commands:
            miner_id = command.get("miner_id")
            if not miner_id or miner_id not in self.miners:
                continue

            if command.get("type") == "switch_pool":
                pool_config = command.get("pool_config") or {}
                label = pool_config.get("label")
                if label:
                    self.miners[str(miner_id)].pool_label = str(label)

            elif command.get("type") == "switch_default":
                self.miners[str(miner_id)].pool_label = str(default_label)

        self._applied_pubsub_index = len(commands)
        return self

    def at(self, ts: datetime) -> "AllocatorScenario":
        self.clock.freeze(ts)
        return self

    def advance(self, *, seconds: float) -> "AllocatorScenario":
        self.clock.advance(seconds=float(seconds))
        return self

    def set_order_status(self, order_id: str, status: str) -> "AllocatorScenario":
        async def _update() -> None:
            await update_order_status(self.db.pool, str(order_id), str(status))

        asyncio.run(_update())
        return self

    async def tick_async(self) -> AllocatorTickResult:
        now = self.clock.now()

        # Load active orders from DB
        marketplace_orders = await get_orders(self.db.pool, status="active")
        active_orders = [o.to_snapshot() for o in marketplace_orders]

        # Current miner snapshots
        active_miners = [m.to_snapshot() for m in self.miners.values()]

        snapshot = AllocationSnapshot(
            now=now,
            orders=active_orders,
            miners=active_miners,
            hashprice_btc_eh_day=None,
        )

        # Load runtime state
        miner_ids = [m.miner_id for m in active_miners]
        order_ids: set[str] = {o.order_id for o in active_orders}
        for miner in active_miners:
            if miner.pool_label not in self.config.default_labels:
                order_ids.add(str(miner.pool_label))

        runtime_view = await self.runtime_state.load_view(
            miner_ids, order_ids=sorted(order_ids)
        )

        # Pool health evaluation
        pool_health = evaluate_pool_health(
            now=now,
            miners=active_miners,
            prev_order_health=runtime_view.order_health,
            prev_miner_share_stats=runtime_view.miner_share_stats,
            config=self.config,
        )
        await self.runtime_state.persist_miner_share_stats(
            pool_health.miner_share_stats
        )
        await self.runtime_state.persist_order_health(pool_health.order_health)

        # Rules
        ctx = PlanningInput(snapshot=snapshot, config=self.config, runtime=runtime_view)
        ctx.order_health = pool_health.order_health

        for rule in default_rules():
            rule.apply(ctx)

        # Plan
        plan, diag = plan_tick(ctx)

        # Execute
        execution = await self.executor.apply(snapshot, plan)

        result = AllocatorTickResult(
            now=now,
            snapshot=snapshot,
            runtime_view=runtime_view,
            pool_health=pool_health,
            plan=plan,
            diagnostics=diag,
            execution=execution,
        )
        self.last_result = result
        return result

    def tick(self) -> AllocatorTickResult:
        return asyncio.run(self.tick_async())

    def when_tick(self) -> "AllocatorScenario":
        self.tick()
        return self

    def expect_moves(self, expected: list[dict[str, Any]]) -> "AllocatorScenario":
        if not self.last_result:
            raise AssertionError("No tick has been run yet")

        actual = [
            {
                "miner_id": m.miner_id,
                "from": m.from_label,
                "to": m.to_order_id,
                "reason": m.reason.value
                if hasattr(m.reason, "value")
                else str(m.reason),
            }
            for m in self.last_result.plan.moves
        ]
        actual_sorted = sorted(actual, key=lambda move: move["miner_id"])

        expected_norm = [
            {
                "miner_id": str(e["miner_id"]),
                "from": str(e.get("from")),
                "to": e.get("to"),
                "reason": (
                    e["reason"].value
                    if isinstance(e.get("reason"), MoveReason)
                    else str(e.get("reason"))
                ),
            }
            for e in expected
        ]
        expected_sorted = sorted(expected_norm, key=lambda move: move["miner_id"])

        assert_equal(
            actual_sorted,
            expected_sorted,
            message=f"[{self.name}] planned moves mismatch",
        )
        return self

    def expect_pool_health_state(
        self, order_id: str, state: PoolHealthState
    ) -> "AllocatorScenario":
        if not self.last_result or not self.last_result.pool_health:
            raise AssertionError("No pool health decision available for last tick")

        health_record = self.last_result.pool_health.order_health.get(str(order_id))
        if not health_record:
            raise AssertionError(
                f"Order {order_id!r} missing from pool health. Known={sorted(self.last_result.pool_health.order_health.keys())}"
            )

        message = (
            f"[{self.name}] expected pool health state {state} for order {order_id}, \
                got {health_record.status} (reason={health_record.reason!r})"
        )
        assert_equal(
            health_record.status,
            state,
            message=message,
        )
        return self

    def expect_published_types(self, expected_types: list[str]) -> "AllocatorScenario":
        actual_types = [
            p.get("type") for p in self.redis.published_json(channel="miner:commands")
        ]
        assert_equal(
            actual_types,
            expected_types,
            message=f"[{self.name}] published command types mismatch",
        )
        return self
