"""
TaoHash Marketplace Allocator Pool Health Evaluation.

This module provides a pool health layer for upstream pools:

  * Uses proxy heartbeats (accepted/rejected counts, reject breakdown,
    last_notify_age_s) to infer whether an upstream pool is doing useful work.
  * Applies hysteresis via EMA smoothing, warmup windows, and backoff.
  * Produces a simple per-order state that the allocator can use to
    temporarily exclude an order (drain miners) when the pool looks unhealthy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.marketplace_allocator.types import AllocatorConfig, MinerSnapshot


class OrderHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARMUP = "WARMUP"
    DEGRADED = "DEGRADED"
    BACKOFF = "BACKOFF"


def _utc_now_ts(now: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return float(now.timestamp())


@dataclass
class MinerCounterRecord:
    """Persistent per-miner counters used to compute deltas between ticks."""

    accepted: int = 0
    rejected: int = 0
    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0
    pool_label: str = "unknown"

    @classmethod
    def from_snapshot(cls, m: MinerSnapshot) -> "MinerCounterRecord":
        return cls(
            accepted=int(m.accepted or 0),
            rejected=int(m.rejected or 0),
            rejected_stale=int(m.rejected_stale or 0),
            rejected_duplicate=int(m.rejected_duplicate or 0),
            rejected_low_diff=int(m.rejected_low_diff or 0),
            rejected_other=int(m.rejected_other or 0),
            pool_label=str(m.pool_label or "unknown"),
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MinerCounterRecord":
        return cls(
            accepted=int(data.get("accepted") or 0),
            rejected=int(data.get("rejected") or 0),
            rejected_stale=int(data.get("rejected_stale") or 0),
            rejected_duplicate=int(data.get("rejected_duplicate") or 0),
            rejected_low_diff=int(data.get("rejected_low_diff") or 0),
            rejected_other=int(data.get("rejected_other") or 0),
            pool_label=str(data.get("pool_label") or "unknown"),
        )


@dataclass
class OrderHealthRecord:
    """Persistent per-order health state."""

    status: OrderHealthStatus = OrderHealthStatus.HEALTHY
    reason: str = ""

    # Unix timestamps (seconds). 0 means unset.
    warmup_until_ts: float = 0.0 # When warmup ends
    backoff_until_ts: float = 0.0 # When backoff expires

    # Backoff exponent for repeated failures.
    backoff_level: int = 0 # 0-10, 10 is max

    # Smoothed metrics (EMA)
    ema_reject_rate: float = 0.0
    ema_other_reject_rate: float = 0.0
    ema_job_gap_s: float = 0.0

    # Unix timestamp of last evaluation.
    last_update_ts: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "OrderHealthRecord":
        status_raw = str(data.get("status") or OrderHealthStatus.HEALTHY.value)
        try:
            status = OrderHealthStatus(status_raw)
        except Exception:
            status = OrderHealthStatus.HEALTHY
        return cls(
            status=status,
            reason=str(data.get("reason") or ""),
            warmup_until_ts=float(data.get("warmup_until_ts") or 0.0),
            backoff_until_ts=float(data.get("backoff_until_ts") or 0.0),
            backoff_level=int(data.get("backoff_level") or 0),
            ema_reject_rate=float(data.get("ema_reject_rate") or 0.0),
            ema_other_reject_rate=float(data.get("ema_other_reject_rate") or 0.0),
            ema_job_gap_s=float(data.get("ema_job_gap_s") or 0.0),
            last_update_ts=float(data.get("last_update_ts") or 0.0),
        )


@dataclass
class OrderHealthTick:
    """One-tick aggregated deltas for an order."""

    delta_accepted: int = 0
    delta_rejected: int = 0
    delta_other_rejected: int = 0
    delta_total: int = 0

    # Smallest last_notify_age_s across miners on the order.
    # None means not available.
    min_last_notify_age_s: Optional[float] = None

    def observe_notify_age(self, age: Optional[float]) -> None:
        if age is None:
            return
        if self.min_last_notify_age_s is None:
            self.min_last_notify_age_s = float(age)
        else:
            self.min_last_notify_age_s = min(self.min_last_notify_age_s, float(age))


@dataclass
class PoolHealthDecision:
    order_health: dict[str, OrderHealthRecord]
    miner_counters: dict[str, MinerCounterRecord]
    events: list[dict]


def evaluate_pool_health(
    *,
    now: datetime,
    miners: list[MinerSnapshot],
    prev_order_health: dict[str, OrderHealthRecord],
    prev_miner_counters: dict[str, MinerCounterRecord],
    config: AllocatorConfig,
) -> PoolHealthDecision:
    """Evaluate order pool health and compute an updated state."""

    if not config.enable_pool_health:
        miner_counters = {
            m.worker_ip: MinerCounterRecord.from_snapshot(m) for m in miners
        }
        return PoolHealthDecision(
            order_health=dict(prev_order_health),
            miner_counters=miner_counters,
            events=[],
        )

    now_ts = _utc_now_ts(now)
    alpha = float(config.pool_health_ema_alpha)
    alpha = max(0.0, min(1.0, alpha))

    default_labels = set(config.default_labels)

    # ------------------------------------------------------------------
    # 1) Build per-order aggregated deltas from miner cumulative counters
    # ------------------------------------------------------------------
    per_order: dict[str, OrderHealthTick] = {}
    miner_counters: dict[str, MinerCounterRecord] = {}

    for miner in miners:
        worker_ip = str(miner.worker_ip)
        current = MinerCounterRecord.from_snapshot(miner)
        miner_counters[worker_ip] = current

        label = str(miner.pool_label or "unknown")
        if label in default_labels:
            continue

        previous = prev_miner_counters.get(worker_ip)
        # If we don't have history, or the miner just moved pools, don't
        # attribute deltas this tick (avoids misclassifying new pools).
        if previous is None or str(previous.pool_label) != label:
            tick = per_order.setdefault(label, OrderHealthTick())
            tick.observe_notify_age(miner.last_notify_age_s)
            continue

        # Counter resets / restarts.
        # Proxy restarted / miner reconnected
        if (
            current.accepted < previous.accepted
            or current.rejected < previous.rejected
            or current.rejected_other < previous.rejected_other
        ):
            tick = per_order.setdefault(label, OrderHealthTick())
            tick.observe_notify_age(miner.last_notify_age_s)
            continue

        delta_accepted = current.accepted - previous.accepted
        delta_rejected = current.rejected - previous.rejected
        delta_other_rejected = current.rejected_other - previous.rejected_other
        delta_total = max(0, delta_accepted + delta_rejected)

        tick = per_order.setdefault(label, OrderHealthTick())
        tick.delta_accepted += int(max(0, delta_accepted))
        tick.delta_rejected += int(max(0, delta_rejected))
        tick.delta_other_rejected += int(max(0, delta_other_rejected))
        tick.delta_total += int(delta_total)
        tick.observe_notify_age(miner.last_notify_age_s)

    # ------------------------------------------------------------------
    # 2) Update per-order health records
    # ------------------------------------------------------------------
    updated: dict[str, OrderHealthRecord] = dict(prev_order_health)
    events: list[dict] = []

    # Consider only orders that are currently being mined (seen in per_order) or
    # already tracked in state.
    # Union of existing health records and new orders seen this tick.
    relevant_order_ids = set(updated.keys()) | set(per_order.keys())

    for order_id in relevant_order_ids:
        record = updated.get(order_id) or OrderHealthRecord()
        tick = per_order.get(order_id) or OrderHealthTick()

        # Backoff handling
        if record.status == OrderHealthStatus.BACKOFF and record.backoff_until_ts > 0:
            if now_ts < record.backoff_until_ts:
                record.last_update_ts = now_ts
                updated[order_id] = record
                continue
            # Backoff expired; clear and allow allocation again.
            record.status = OrderHealthStatus.HEALTHY
            record.reason = ""
            record.backoff_until_ts = 0.0
            record.warmup_until_ts = 0.0
            # Keep backoff_level so repeated failures escalate.

        # Start warmup when we first see activity (miners on the order) and
        # we don't already have a warmup window.
        has_activity = tick.min_last_notify_age_s is not None or tick.delta_total > 0
        if has_activity and record.warmup_until_ts <= 0:
            record.status = OrderHealthStatus.WARMUP
            record.warmup_until_ts = now_ts + float(config.pool_health_warmup_seconds)
            record.reason = "warmup"

        # Warmup completion
        if record.status == OrderHealthStatus.WARMUP:
            if record.warmup_until_ts > 0 and now_ts >= record.warmup_until_ts:
                record.status = OrderHealthStatus.HEALTHY
                record.reason = ""
                record.warmup_until_ts = 0.0

        # Always update job-gap EMA if we have notify ages
        if tick.min_last_notify_age_s is not None:
            if record.ema_job_gap_s <= 0:
                record.ema_job_gap_s = float(tick.min_last_notify_age_s)
            else:
                record.ema_job_gap_s = (
                    1 - alpha
                ) * record.ema_job_gap_s + alpha * float(tick.min_last_notify_age_s)

        # Hard unhealthy signal: no jobs for too long.
        if tick.min_last_notify_age_s is not None and float(
            tick.min_last_notify_age_s
        ) >= float(config.pool_health_no_notify_seconds):
            # Trigger backoff immediately (even during warmup).
            base = int(config.pool_health_backoff_base_seconds)
            max_b = int(config.pool_health_backoff_max_seconds)
            level = max(0, int(record.backoff_level))
            backoff_s = min(max_b, base * (2**level))
            record.status = OrderHealthStatus.BACKOFF
            record.reason = f"no_jobs_for_{int(tick.min_last_notify_age_s)}s"
            record.backoff_until_ts = now_ts + float(backoff_s)
            record.warmup_until_ts = 0.0
            record.backoff_level = min(level + 1, 10)
            record.last_update_ts = now_ts
            updated[order_id] = record
            events.append(
                {
                    "level": "warning",
                    "code": "POOL_NO_JOBS",
                    "order_id": order_id,
                    "message": "Pool appears to be not sending jobs; placing order in backoff",
                    "data": {
                        "min_last_notify_age_s": tick.min_last_notify_age_s,
                        "backoff_seconds": backoff_s,
                        "backoff_level": record.backoff_level,
                    },
                }
            )
            continue

        # Shares-based evaluation (skip during warmup)
        if record.status != OrderHealthStatus.WARMUP and tick.delta_total >= int(
            config.pool_health_min_samples
        ):
            reject_rate = float(tick.delta_rejected) / float(max(1, tick.delta_total))
            other_rate = float(tick.delta_other_rejected) / float(
                max(1, tick.delta_total)
            )

            if record.ema_reject_rate <= 0:
                record.ema_reject_rate = reject_rate
            else:
                record.ema_reject_rate = (
                    1 - alpha
                ) * record.ema_reject_rate + alpha * reject_rate

            if record.ema_other_reject_rate <= 0:
                record.ema_other_reject_rate = other_rate
            else:
                record.ema_other_reject_rate = (
                    1 - alpha
                ) * record.ema_other_reject_rate + alpha * other_rate

            # If unknown/other rejects are consistently high, treat as unhealthy.
            if record.ema_other_reject_rate >= float(
                config.pool_health_other_reject_threshold
            ):
                base = int(config.pool_health_backoff_base_seconds)
                max_b = int(config.pool_health_backoff_max_seconds)
                level = max(0, int(record.backoff_level))
                backoff_s = min(max_b, base * (2**level))
                record.status = OrderHealthStatus.BACKOFF
                record.reason = "high_unknown_rejects"
                record.backoff_until_ts = now_ts + float(backoff_s)
                record.backoff_level = min(level + 1, 10)
                events.append(
                    {
                        "level": "warning",
                        "code": "POOL_UNHEALTHY_REJECTS",
                        "order_id": order_id,
                        "message": "Pool appears unhealthy (high other rejects); placing order in backoff",
                        "data": {
                            "delta_total": tick.delta_total,
                            "delta_rejected": tick.delta_rejected,
                            "delta_other_rejected": tick.delta_other_rejected,
                            "ema_other_reject_rate": record.ema_other_reject_rate,
                            "threshold": config.pool_health_other_reject_threshold,
                            "backoff_seconds": backoff_s,
                            "backoff_level": record.backoff_level,
                        },
                    }
                )

            elif record.ema_reject_rate >= float(config.pool_health_reject_threshold):
                # Degraded but not drained by default. Useful for dashboards.
                if record.status != OrderHealthStatus.DEGRADED:
                    record.status = OrderHealthStatus.DEGRADED
                    record.reason = "high_reject_rate"
                    events.append(
                        {
                            "level": "info",
                            "code": "POOL_DEGRADED",
                            "order_id": order_id,
                            "message": "Pool appears degraded (high reject rate)",
                            "data": {
                                "delta_total": tick.delta_total,
                                "ema_reject_rate": record.ema_reject_rate,
                                "threshold": config.pool_health_reject_threshold,
                            },
                        }
                    )
            else:
                # Recovery
                if record.status == OrderHealthStatus.DEGRADED:
                    record.status = OrderHealthStatus.HEALTHY
                    record.reason = ""

        record.last_update_ts = now_ts
        updated[order_id] = record

    return PoolHealthDecision(
        order_health=updated, miner_counters=miner_counters, events=events
    )
