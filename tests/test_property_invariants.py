from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import random

import pytest

from src.marketplace_allocator.types import AllocatorConfig
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.scenarios.planner import PlannerScenario


pytest.importorskip("redis", reason="property tests require redis-py (library only)")
hypothesis = pytest.importorskip("hypothesis")
st = pytest.importorskip("hypothesis.strategies")


def _order_id(n: int) -> str:
    return f"order_{n}"


def _miner_id(n: int) -> str:
    return f"miner_{n}"


@hypothesis.given(
    order_count=st.integers(min_value=0, max_value=4),
    miner_count=st.integers(min_value=1, max_value=6),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_plan_invariants(order_count: int, miner_count: int, seed: int) -> None:
    """Planner output should satisfy basic safety invariants.

    Tests probe a wide range of small, semi-random inputs and checks
    that the planner never produces an invalid plan
    (duplicate moves, moves to unknown orders, etc.).
    """

    config = AllocatorConfig(
        rebalance_rate=1.0,  # faster moves
        overshoot_pct=0.15,
        max_switches_per_tick=10,
    )
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    rng = random.Random(int(seed))

    scenario = PlannerScenario("invariants", config=config).at(now)

    # Random orders
    for i in range(order_count):
        price = Decimal("0.05") + Decimal(str(rng.random())) * Decimal("0.25")
        limit = Decimal("0.05") + Decimal(str(rng.random())) * Decimal("0.30")
        scenario.given_order(
            _order_id(i),
            price_btc_eh_day=price,
            balance_btc=Decimal("1"),
            limit_eh_s=limit,
        )

    # Miners: some idle, some assigned to existing orders. 
    # Some parked on unknown label to exercise hard-drain path.
    for i in range(miner_count):
        hashrate = 0.03 + 0.07 * rng.random()

        label_choices = ["normal"]
        if order_count > 0:
            label_choices.extend([_order_id(j) for j in range(order_count)])
        label_choices.append("unknown")

        label = rng.choice(label_choices)
        scenario.given_miner(SimulatedMiner(_miner_id(i), hashrate, pool_label=label))

    result = scenario.tick()
    moves = result.plan.moves

    # Assert no more than max_switches_per_tick moves
    assert len(moves) <= config.max_switches_per_tick

    # Assert each miner is moved at most once per plan
    moved_ids = [m.miner_id for m in moves]
    assert len(moved_ids) == len(set(moved_ids))

    # Assert every move references an existing miner
    miner_ids = {m.miner_id for m in scenario.miners.values()}
    for move in moves:
        assert move.miner_id in miner_ids

    # Assert moves only target existing orders (or default/None)
    order_ids = {o.order_id for o in scenario.orders.values()}
    for move in moves:
        if move.to_order_id is None:
            continue
        assert move.to_order_id in order_ids

    # Assert no-op moves should not be emitted
    for move in moves:
        if move.to_order_id is None:
            assert move.from_label != "normal"
        else:
            assert move.to_order_id != move.from_label
