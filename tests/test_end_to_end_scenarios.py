from datetime import datetime, timezone
from decimal import Decimal

import pytest

pytest.importorskip(
    "asyncpg", reason="end-to-end allocator scenarios require asyncpg (library only)"
)
pytest.importorskip(
    "redis", reason="end-to-end allocator scenarios require redis-py (library only)"
)

from src.marketplace_allocator.pool_health import PoolHealthState
from src.marketplace_allocator.types import AllocatorConfig, MoveReason
from tests.framework.scenarios.allocator import AllocatorScenario


def test_e2e_order_lifecycle_allocate_then_fulfill_drains_miners():
    """Allocate into an order, then fulfill it, and ensure miners are drained.

    Once an order is marked fulfilled, it drops out of the active set and
    the next tick should evacuate any miners still pointed at that pool label.
    """
    config = AllocatorConfig(rebalance_rate=1.0, overshoot_pct=0.0)
    scenario = (
        AllocatorScenario("order lifecycle", config=config)
        .at(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        .given_order(
            "order_1",
            price_btc_eh_day=Decimal("0.20"),
            amount_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_miner("miner_1", hashrate_eh_s=0.10, pool_label="normal")
        .given_miner("miner_2", hashrate_eh_s=0.10, pool_label="normal")
    )

    # Allocate a miner
    scenario.when_tick().expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "normal",
                "to": "order_1",
                "reason": MoveReason.MARKET_REBALANCE,
            }
        ]
    )

    scenario.apply_new_pubsub_commands()

    # Fulfill the order - next tick should hard drain.
    scenario.set_order_status("order_1", "fulfilled")
    scenario.advance(seconds=30).when_tick().expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "order_1",
                "to": None,
                "reason": MoveReason.HARD_DRAIN,
            }
        ]
    )


def test_e2e_pool_backoff_hard_drains_order_and_prevents_reallocation():
    """A pool health BACKOFF should hard-drain miners and block reallocation.

    Pool-health is computed from deltas of cumulative counters.
    When a miner switches pools, the first tick on the new label acts
    as a baseline and cannot produce a delta.
    """
    config = AllocatorConfig(
        rebalance_rate=1.0,
        overshoot_pct=0.0,
        pool_health_warmup_seconds=0,
        pool_health_min_samples=10,
        pool_health_ema_alpha=1.0,
        pool_health_other_reject_threshold=0.20,
    )

    scenario = (
        AllocatorScenario("pool backoff", config=config)
        .at(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        .given_order(
            "order_1",
            price_btc_eh_day=Decimal("0.20"),
            amount_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_miner("miner_1", hashrate_eh_s=0.10, pool_label="normal")
    )

    # First tick: allocate the miner to the order.
    scenario.when_tick().expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "normal",
                "to": "order_1",
                "reason": MoveReason.MARKET_REBALANCE,
            }
        ]
    )
    scenario.apply_new_pubsub_commands()
    scenario.advance(seconds=30).when_tick().expect_moves([])

    # Next tick: simulate unhealthy pool - should enter BACKOFF and drain.
    scenario.miner("miner_1").notify_age(1).mine_shares(rejected=100, rejected_other=40)
    scenario.advance(seconds=30).when_tick().expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "order_1",
                "to": None,
                "reason": MoveReason.HARD_DRAIN,
            }
        ]
    )

    scenario.expect_pool_health_state("order_1", PoolHealthState.BACKOFF)


def test_e2e_cooldown_prevents_thrashing_then_allows_after_time_advances():
    """Cooldown blocks immediate rebalances and expires deterministically."""
    config = AllocatorConfig(
        rebalance_rate=1.0,
        overshoot_pct=0.0,
        miner_cooldown_seconds=600,
    )

    scenario = (
        AllocatorScenario("cooldown thrash", config=config)
        .at(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        .given_order(
            "low",
            price_btc_eh_day=Decimal("0.10"),
            amount_btc=Decimal("1"),
            limit_eh_s=Decimal("0.10"),
        )
        .given_miner("miner_1", hashrate_eh_s=0.10, pool_label="normal")
    )

    # Allocate miner to low.
    scenario.when_tick().expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "normal",
                "to": "low",
                "reason": MoveReason.MARKET_REBALANCE,
            }
        ]
    )
    scenario.apply_new_pubsub_commands()

    # New higher-paying order arrives.
    scenario.given_order(
        "high",
        price_btc_eh_day=Decimal("0.20"),
        amount_btc=Decimal("1"),
        limit_eh_s=Decimal("0.10"),
    )

    # Planner would like to move to high, but miner is in cooldown.
    scenario.advance(seconds=30).when_tick().expect_moves([])

    # After cooldown expires - the next tick can move the miner.
    scenario.advance(seconds=601).when_tick().expect_moves(
        [
            {
                "miner_id": "miner_1",
                "from": "low",
                "to": "high",
                "reason": MoveReason.MARKET_REBALANCE,
            }
        ]
    )
