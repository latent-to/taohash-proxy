from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from src.marketplace_allocator.planner import plan_tick
from src.marketplace_allocator.pool_health import PoolHealthRecord
from src.marketplace_allocator.rules import PlanningInput, default_rules
from src.marketplace_allocator.runtime_state import RuntimeStateSnapshot
from src.marketplace_allocator.types import AllocationSnapshot, AllocatorConfig, MoveReason
from tests.framework.assertions import assert_equal
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.builders.order import SimulatedOrder
from tests.framework.clock import DeterministicClock


@dataclass
class PlannerTickResult:
    now: datetime
    plan: Any
    diagnostics: Any


class PlannerScenario:
    """Run the rules pipeline and planner against an in-memory snapshot.
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

        self.orders: dict[str, SimulatedOrder] = {}
        self.miners: dict[str, SimulatedMiner] = {}

        # Runtime inputs
        self.last_tick_ts: Optional[datetime] = None
        self.miner_last_move_at: dict[str, datetime] = {}
        self.order_health: dict[str, PoolHealthRecord] = {}

        self.last_result: Optional[PlannerTickResult] = None

    def given_order(
        self,
        order_id: str,
        *,
        price_btc_eh_day: Decimal,
        balance_btc: Decimal,
        limit_eh_s: Optional[Decimal] = None,
        status: str = "active",
        queue_position_time: Optional[datetime] = None,
    ) -> "PlannerScenario":
        self.orders[order_id] = SimulatedOrder(
            order_id=order_id,
            price_btc_eh_day=price_btc_eh_day,
            balance_btc=balance_btc,
            limit_eh_s=limit_eh_s,
            status=status,
            queue_position_time=queue_position_time,
        )
        return self

    def given_miner(self, miner: SimulatedMiner) -> "PlannerScenario":
        self.miners[miner.miner_id] = miner
        return self

    def given_last_tick(self, ts: datetime) -> "PlannerScenario":
        self.last_tick_ts = ts
        return self

    def given_miner_last_moved(self, miner_id: str, moved_at: datetime) -> "PlannerScenario":
        self.miner_last_move_at[str(miner_id)] = moved_at
        return self

    def given_order_health(self, order_id: str, record: PoolHealthRecord) -> "PlannerScenario":
        self.order_health[str(order_id)] = record
        return self

    def at(self, ts: datetime) -> "PlannerScenario":
        self.clock.freeze(ts)
        return self

    def advance(self, *, seconds: float) -> "PlannerScenario":
        self.clock.advance(seconds=float(seconds))
        return self

    def tick(self) -> PlannerTickResult:
        now = self.clock.now()
        snapshot = AllocationSnapshot(
            now=now,
            orders=[o.to_snapshot(now=now) for o in self.orders.values()],
            miners=[m.to_snapshot() for m in self.miners.values()],
        )
        runtime = RuntimeStateSnapshot(
            last_tick_ts=self.last_tick_ts,
            miner_last_move_at=dict(self.miner_last_move_at),
            miner_share_stats={},
            order_health=dict(self.order_health),
        )

        ctx = PlanningInput(snapshot=snapshot, config=self.config, runtime=runtime)
        ctx.order_health = dict(self.order_health)

        for rule in default_rules():
            rule.apply(ctx)

        plan, diag = plan_tick(ctx)
        self.last_result = PlannerTickResult(now=now, plan=plan, diagnostics=diag)
        return self.last_result

    def expect_moves(self, expected: list[dict[str, Any]]) -> "PlannerScenario":
        if not self.last_result:
            raise AssertionError("No tick has been run yet")

        actual = [
            {
                "miner_id": m.miner_id,
                "from": m.from_label,
                "to": m.to_order_id,
                "reason": m.reason.value if hasattr(m.reason, "value") else str(m.reason),
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

    def expect_planned_switches(self, expected: int) -> "PlannerScenario":
        if not self.last_result:
            raise AssertionError("No tick has been run yet")
        assert_equal(
            int(self.last_result.diagnostics.planned_switches),
            int(expected),
            message=f"[{self.name}] planned_switches mismatch",
        )
        return self
