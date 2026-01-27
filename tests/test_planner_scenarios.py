from datetime import datetime, timezone
from decimal import Decimal

import pytest

pytest.importorskip("redis", reason="planner scenarios require redis-py (library only)")

from src.marketplace_allocator.types import AllocatorConfig, MoveReason
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.scenarios.planner import PlannerScenario


def test_planner_prefers_higher_price_orders_and_moves_miners():
    """A miner on a lower-priced order should be moved to a higher-priced one.

    This is the simplest "price wins" scenario: both orders are eligible and
    have the same limit, but one pays more. With no cooldowns or damping, the
    planner should emit a single MARKET_REBALANCE move.
    """
    config = AllocatorConfig(rebalance_rate=1.0, overshoot_pct=0.0)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    scenario = (
        PlannerScenario("price_ordering", config=config)
        .at(now)
        .given_order(
            "high",
            price_btc_eh_day=Decimal("0.20"),
            balance_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_order(
            "low",
            price_btc_eh_day=Decimal("0.10"),
            balance_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_miner(SimulatedMiner("miner_1", 0.10, pool_label="low"))
    )

    scenario.tick()
    scenario.expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "low",
                "to": "high",
                "reason": MoveReason.MARKET_REBALANCE,
            }
        ]
    )


def test_planner_respects_miner_cooldown_and_skips_move():
    """Cooldown should suppress market rebalances for recently moved miners."""
    config = AllocatorConfig(
        rebalance_rate=1.0, overshoot_pct=0.0, miner_cooldown_seconds=600
    )
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    scenario = (
        PlannerScenario("cooldown", config=config)
        .at(now)
        .given_order(
            "high",
            price_btc_eh_day=Decimal("0.20"),
            balance_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_order(
            "low",
            price_btc_eh_day=Decimal("0.10"),
            balance_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_miner(SimulatedMiner("miner_1", 0.10, pool_label="low"))
        .given_miner_last_moved("miner_1", now)  # just moved
    )

    scenario.tick()
    scenario.expect_moves([])


def test_planner_hard_drains_ineligible_orders_even_during_cooldown():
    """Hard-drain overrides cooldown.

    If an order becomes ineligible (for example, its balance hits zero), miners
    must be drained immediately even if they'd normally be protected by the
    cooldown window.
    """
    config = AllocatorConfig(
        rebalance_rate=0.0, overshoot_pct=0.0, miner_cooldown_seconds=600
    )
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    scenario = (
        PlannerScenario("hard_drain", config=config)
        .at(now)
        .given_order(
            "dead",
            price_btc_eh_day=Decimal("0.20"),
            balance_btc=Decimal("0"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_miner(SimulatedMiner("miner_1", 0.10, pool_label="dead"))
        .given_miner_last_moved("miner_1", now)  # would normally be in cooldown
    )

    scenario.tick()
    scenario.expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "dead",
                "to": None,
                "reason": MoveReason.HARD_DRAIN,
            }
        ]
    )
