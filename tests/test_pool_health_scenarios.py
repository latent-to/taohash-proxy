from datetime import datetime, timezone

from src.marketplace_allocator.pool_health import PoolHealthState
from src.marketplace_allocator.types import AllocatorConfig
from tests.framework.builders.miner import SimulatedMiner
from tests.framework.scenarios.pool_health import PoolHealthScenario


def test_pool_health_warmup_transitions_to_healthy_and_stays_healthy():
    """Warmup should complete once and not restart on normal activity.

    Start with a miner already hashing on an order. The first evaluation
    should put the order into WARMUP. After the warmup window elapses, the
    order should become HEALTHY. Continued shares should keep it HEALTHY.
    """
    config = AllocatorConfig(
        pool_health_warmup_seconds=10,
        pool_health_min_samples=20,
        pool_health_ema_alpha=1.0,
    )

    scenario = PoolHealthScenario("warmup->healthy", config=config).at(
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    )

    scenario.given_miner(
        SimulatedMiner("miner_1", 0.1, pool_label="order_1")
        .notify_age(1)
        .mine_shares(accepted=5)
    )

    scenario.tick()
    scenario.expect_order_state("order_1", PoolHealthState.WARMUP)

    # Warmup expires
    scenario.advance(seconds=11)
    scenario.tick()
    scenario.expect_order_state("order_1", PoolHealthState.HEALTHY)

    # Continuous activity - stays in HEALTHY
    scenario.advance(seconds=30)
    scenario.miners["miner_1"].mine_shares(accepted=5).notify_age(1)
    scenario.tick()
    scenario.expect_order_state("order_1", PoolHealthState.HEALTHY)


def test_pool_health_high_other_rejects_triggers_backoff():
    """High 'other' rejects should trigger BACKOFF and keep it sticky.

    The evaluator uses per-tick deltas from cumulative counters, so we take an
    initial baseline tick before injecting rejects.
    """
    config = AllocatorConfig(
        pool_health_warmup_seconds=0,
        pool_health_min_samples=10,
        pool_health_ema_alpha=1.0,
        pool_health_other_reject_threshold=0.20,
    )

    scenario = PoolHealthScenario("other_rejects->backoff", config=config)
    scenario.given_miner(
        SimulatedMiner("miner_1", 0.1, pool_label="order_1").notify_age(1)
    )
    scenario.tick()

    # Burst of "other" rejects
    scenario.miners["miner_1"].mine_shares(rejected=100, rejected_other=40).notify_age(
        1
    )

    scenario.tick()
    scenario.expect_order_state("order_1", PoolHealthState.BACKOFF)
    scenario.expect_backoff_until_after_now("order_1")

    first_backoff_until_ts = scenario.last_result.decision.order_health[
        "order_1"
    ].backoff_until_ts

    # Backoff remains until expiry
    scenario.advance(seconds=30)
    scenario.miners["miner_1"].mine_shares(accepted=10).notify_age(1)
    scenario.tick()
    scenario.expect_order_state("order_1", PoolHealthState.BACKOFF)
    assert (
        scenario.last_result.decision.order_health["order_1"].backoff_until_ts
        == first_backoff_until_ts
    )


def test_pool_health_high_reject_rate_marks_degraded_and_recovers():
    """A high reject-rate delta should degrade, then recover after enough good ticks.

    The pool-health state machine is conservative when recovering.
    Once an order is DEGRADED, it requires multiple consecutive GOOD ticks
    before returning to HEALTHY.

    Steps:
      1) Establish a baseline tick so deltas can be computed.
      2) Emit a high reject-rate delta (but not "other rejects") -> DEGRADED.
      3) Emit several clean deltas -> HEALTHY.
    """
    config = AllocatorConfig(
        pool_health_warmup_seconds=0,
        pool_health_min_samples=10,
        pool_health_ema_alpha=1.0,
        pool_health_other_reject_threshold=0.90,
        pool_health_reject_threshold=0.50,
    )

    scenario = PoolHealthScenario("rejects->degraded", config=config)
    scenario.given_miner(
        SimulatedMiner("miner_1", 0.1, pool_label="order_1").notify_age(1)
    )
    scenario.tick()

    # High reject rate - DEGRADED
    scenario.miners["miner_1"].mine_shares(rejected=100, rejected_other=0).notify_age(1)
    scenario.tick()
    scenario.expect_order_state("order_1", PoolHealthState.DEGRADED)

    # Recovery requires multiple good ticks
    for _ in range(3):
        scenario.advance(seconds=30)
        scenario.miners["miner_1"].mine_shares(accepted=100).notify_age(1)
        scenario.tick()

    scenario.expect_order_state("order_1", PoolHealthState.HEALTHY)
