"""Marketplace allocator engine.

This package contains the core allocator engine for the TaoHash hashrate marketplace.

* types.py
    Core dataclasses for snapshots, plans, and diagnostics.

* runtime_state.py
    Lightweight runtime state (cooldowns, warmup windows, backoff) stored in
    Redis so allocator decisions remain stable across restarts.

* rules.py
    A small rule pipeline that turns a raw snapshot into allocator constraints
    (eligibility, hard-drains, movement limits, etc.).

* planner.py
    The deterministic allocation algorithm (damping, target
    computation, miner selection).

* executor.py
    Applies a plan: updates Postgres assignments and publishes Redis switch
    commands to proxies.

* telemetry.py
    Emits allocator state/decisions to Redis for debugging.
"""

from .types import (
    AllocatorConfig,
    AllocationPlan,
    AllocationSnapshot,
    DecisionEvent,
    MinerSnapshot,
    OrderSnapshot,
)

__all__ = [
    "AllocatorConfig",
    "AllocationPlan",
    "AllocationSnapshot",
    "DecisionEvent",
    "MinerSnapshot",
    "OrderSnapshot",
]
