from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from src.marketplace_allocator.types import MinerSnapshot
from src.marketplace_allocator.rules import PlanningContext
from src.marketplace_allocator.types import (
    AllocationPlan,
    MoveReason,
    OrderDiagnostics,
    PlanMove,
    TickDiagnostics,
)


@dataclass
class _AvailableMiners:
    """Categorized miners available to donate hashrate to receivers.

    Miners are drawn in priority order: idle -> forced -> soft.

    Attributes:
        idle: Miners on default labels (not assigned to any order). First choice
              since moving them doesn't disrupt existing allocations.
        forced: Miners on hard-drain orders (ineligible/unhealthy). Must be moved
                regardless of cooldown - these orders cannot receive hashrate.
        soft_by_order: Miners on oversupplied eligible orders, keyed by order_id.
                       Lowest-priority orders moved first to minimize disruption
                       to high-paying orders.
    """

    idle: list["MinerSnapshot"]
    forced: list["MinerSnapshot"]
    soft_by_order: dict[str, list["MinerSnapshot"]]


def _compute_alpha(rate: float, dt_seconds: float, interval_seconds: float) -> float:
    """
    Compute exponential smoothing coefficient.

    alpha = 1 - (1 - rate)^(dt/interval)
    """

    if interval_seconds <= 0:
        return 1.0
    if rate <= 0:
        return 0.0
    if rate >= 1:
        return 1.0
    # Guard against floating issues for extreme dt.
    exponent = max(0.0, float(dt_seconds) / float(interval_seconds))
    return 1.0 - math.pow(1.0 - float(rate), exponent)


def _rank_orders(ctx: PlanningContext):
    """Sort eligible orders by priority: highest price first, then earliest queue time."""
    return sorted(
        ctx.eligible_orders,
        key=lambda order: (-order.price_btc_eh_day, order.queue_position_time),
    )


def _is_default_label(ctx: PlanningContext, label: str) -> bool:
    """Check if a pool label is a default/idle label (not assigned to any order)."""
    return label in ctx.config.default_labels


def _order_id_from_label(ctx: PlanningContext, label: str) -> Optional[str]:
    """Extract order_id from a pool label. Returns None if it's a default label."""
    if _is_default_label(ctx, label):
        return None
    return label

