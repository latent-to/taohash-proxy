from decimal import Decimal

import pytest

pytest.importorskip("asyncpg", reason="executor scenarios require asyncpg (library only)")
pytest.importorskip("redis", reason="executor scenarios require redis-py (library only)")

from src.marketplace_allocator.types import AllocationPlan, MoveReason, PlanMove
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.builders.order import SimulatedOrder
from tests.framework.scenarios.executor import ExecutorScenario


def test_executor_creates_assignment_and_publishes_switch_pool():
    """Applying a switch-to-order plan should create an assignment and publish a command."""
    scenario = ExecutorScenario("assign_and_publish")

    scenario.given_order(
        SimulatedOrder(
            order_id="order_1",
            price_btc_eh_day=Decimal("0.20"),
            balance_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
    )
    scenario.given_miner(SimulatedMiner("miner_1", 0.10, pool_label="normal"))

    plan = AllocationPlan(
        moves=[
            PlanMove(
                miner_id="miner_1",
                from_label="normal",
                to_order_id="order_1",
                reason=MoveReason.MARKET_REBALANCE,
                hashrate_eh_s=0.10,
            )
        ]
    )

    scenario.apply(plan)
    scenario.expect_execution_counts(moved=1, failed=0)
    scenario.expect_published_command_types(["switch_pool"])

    assignments = scenario.db.active_assignments_by_miner()
    assert assignments["miner_1"]["order_id"] == "order_1"


def test_executor_switch_default_completes_assignment_and_publishes():
    """Switching a miner back to default should close the active assignment and publish."""
    scenario = ExecutorScenario("switch_default")

    scenario.given_order(
        SimulatedOrder(
            order_id="order_1",
            price_btc_eh_day=Decimal("0.20"),
            balance_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
    )
    scenario.given_miner(SimulatedMiner("miner_1", 0.10, pool_label="normal"))

    # Create assignment
    plan_to_order = AllocationPlan(
        moves=[
            PlanMove(
                miner_id="miner_1",
                from_label="normal",
                to_order_id="order_1",
                reason=MoveReason.MARKET_REBALANCE,
                hashrate_eh_s=0.10,
            )
        ]
    )
    scenario.apply(plan_to_order)

    # Drain to default
    plan_to_default = AllocationPlan(
        moves=[
            PlanMove(
                miner_id="miner_1",
                from_label="order_1",
                to_order_id=None,
                reason=MoveReason.HARD_DRAIN,
                hashrate_eh_s=0.10,
            )
        ]
    )
    scenario.apply(plan_to_default)

    scenario.expect_execution_counts(moved=1, failed=0)
    scenario.expect_published_command_types(["switch_pool", "switch_default"])

    assignments = scenario.db.active_assignments_by_miner()
    assert "miner_1" not in assignments
