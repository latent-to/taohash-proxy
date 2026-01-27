from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.marketplace_allocator.types import MinerSnapshot


@dataclass
class SimulatedMiner:
    """Mutable miner model used by scenario tests.

    This model stores cumulative share counters, like real miners.
    Scenario tests can increment these counters step by step to simulate new work each tick.
    """

    miner_id: str
    hashrate_eh_s: float
    pool_label: str = "normal"

    accepted: int = 0
    rejected: int = 0
    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0

    last_notify_age_s: Optional[float] = None
    worker_name: str = ""

    def to_snapshot(self) -> MinerSnapshot:
        return MinerSnapshot(
            miner_id=self.miner_id,
            hashrate_eh_s=float(self.hashrate_eh_s),
            pool_label=str(self.pool_label),
            accepted=int(self.accepted),
            rejected=int(self.rejected),
            rejected_stale=int(self.rejected_stale),
            rejected_duplicate=int(self.rejected_duplicate),
            rejected_low_diff=int(self.rejected_low_diff),
            rejected_other=int(self.rejected_other),
            last_notify_age_s=(
                float(self.last_notify_age_s)
                if self.last_notify_age_s is not None
                else None
            ),
            worker_name=str(self.worker_name or ""),
        )

    def on_label(self, label: str) -> "SimulatedMiner":
        self.pool_label = str(label)
        return self

    def with_hashrate(self, exahash_per_second: float) -> "SimulatedMiner":
        self.hashrate_eh_s = float(exahash_per_second)
        return self

    def notify_age(self, seconds: Optional[float]) -> "SimulatedMiner":
        self.last_notify_age_s = float(seconds) if seconds is not None else None
        return self

    def mine_shares(
        self,
        *,
        accepted: int = 0,
        rejected: int = 0,
        rejected_stale: int = 0,
        rejected_duplicate: int = 0,
        rejected_low_diff: int = 0,
        rejected_other: int = 0,
    ) -> "SimulatedMiner":
        """Increment cumulative share counters."""
        self.accepted += int(accepted)
        self.rejected += int(rejected)
        self.rejected_stale += int(rejected_stale)
        self.rejected_duplicate += int(rejected_duplicate)
        self.rejected_low_diff += int(rejected_low_diff)
        self.rejected_other += int(rejected_other)
        return self
