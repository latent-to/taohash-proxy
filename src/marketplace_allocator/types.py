"""
Allocator datatypes.

These dataclasses are used to:
  * build a read-only snapshot of the current state (orders + miners + hashprice)
  * compute a deterministic plan (miner moves + order actions)
  * export diagnostics for logs / dashboards
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


DEFAULT_LABELS = {"normal", "high_diff", "unknown"}


def gh_to_eh(gh_s: float) -> float:
    """Convert GH/s to EH/s."""

    return float(gh_s) / 1e9


class MoveReason(str, Enum):
    """Why the allocator decided to move a miner."""

    MARKET_REBALANCE = "MARKET_REBALANCE"
    HARD_DRAIN = "HARD_DRAIN"
    CAP_ENFORCEMENT = "CAP_ENFORCEMENT"
    RECONCILE = "RECONCILE"

