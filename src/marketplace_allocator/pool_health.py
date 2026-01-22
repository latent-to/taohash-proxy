"""
TaoHash Marketplace Allocator Pool Health Evaluation.

This module evaluates the health of upstream mining pools by analyzing miner
share statistics (accepted/rejected counts) and job notification patterns.

Key Features:
  * Monitors pool health via proxy heartbeats (share counts, reject breakdown,
    last_notify_age_s) to detect when upstream pools are failing
  * Applies hysteresis via EMA smoothing, warmup windows, and exponential backoff
  * Produces per-order health status (HEALTHY/WARMUP/DEGRADED/BACKOFF) used by
    the allocator to temporarily exclude unhealthy orders

Used by:
  * Allocator loop: Evaluates health before each planning cycle
  * Rules pipeline: PoolHealthRule uses BACKOFF status to exclude orders
  * Planner: Hard-drains orders in BACKOFF state
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.marketplace_allocator.types import AllocatorConfig, MinerSnapshot


class OrderHealthStatus(str, Enum):
    """Health status of an upstream mining pool.

    Used to determine whether an order can receive hashrate. Orders in BACKOFF
    are temporarily excluded from allocation until the backoff period expires.

    Values:
        HEALTHY: Pool is functioning normally and can receive hashrate
        WARMUP: Pool just started receiving hashrate (waiting for initial samples)
        DEGRADED: Pool has high reject rate but still receiving hashrate
        BACKOFF: Pool is unhealthy (no jobs, high unknown rejects) - miners drained
    """

    HEALTHY = "HEALTHY"
    WARMUP = "WARMUP"
    DEGRADED = "DEGRADED"
    BACKOFF = "BACKOFF"


def _utc_now_ts(now: datetime) -> float:
    """Convert datetime to Unix timestamp (UTC).
    
    Ensures the datetime has UTC timezone before converting. Used for storing
    timestamps in health records (warmup_until_ts, backoff_until_ts).
    
    Args:
        now: Datetime to convert (assumed UTC if no timezone)
    
    Returns:
        Unix timestamp in seconds (float)
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return float(now.timestamp())


def _clamp01(x: float) -> float:
    """Convert datetime to Unix timestamp (UTC).
    
    Ensures the datetime has UTC timezone before converting. Used for storing
    timestamps in health records (warmup_until_ts, backoff_until_ts).
    
    Args:
        now: Datetime to convert (assumed UTC if no timezone)
    
    Returns:
        Unix timestamp in seconds (float)
    """
    return max(0.0, min(1.0, float(x)))


def _ema_update(prev_ema: float, alpha: float, sample: float) -> float:
    """Update exponential moving average (EMA) with a new sample.
    
    Computes the next EMA value using the formula:
    EMA_new = (1 - alpha) * EMA_prev + alpha * sample
    
    Used to smooth reject rates and job gaps over time, preventing false
    positives from temporary spikes.
    
    Args:
        prev_ema: Previous EMA value (0.0 if first sample)
        alpha: Smoothing factor (0-1), higher = more responsive to changes
        sample: New sample value
    
    Returns:
        Updated EMA value. Returns the sample if prev_ema <= 0 (first sample).
    """
    if prev_ema <= 0:
        return float(sample)
    return (1.0 - float(alpha)) * float(prev_ema) + float(alpha) * float(sample)


def _compute_backoff_seconds(
    record: "OrderHealthRecord", config: AllocatorConfig
) -> int:
    """Compute backoff duration using exponential backoff.
    
    Calculates backoff duration based on the order's backoff level. Uses
    exponential backoff (base * 2^level) to increase wait time after
    repeated failures. Prevents rapid retries on persistently failing pools.
    
    Args:
        record: Order health record containing backoff_level
        config: Allocator config with backoff base and max settings
    
    Returns:
        Backoff duration in seconds. Capped at config.pool_health_backoff_max_seconds.
        Formula: min(max_seconds, base_seconds * 2^backoff_level)
    """
    base = int(config.pool_health_backoff_base_seconds)
    max_b = int(config.pool_health_backoff_max_seconds)
    level = max(0, int(record.backoff_level))
    return int(min(max_b, base * (2**level)))


@dataclass
class MinerCounterRecord:
    """Persistent per-miner cumulative share statistics.
    
    Tracks cumulative share counts (accepted/rejected) for each miner across
    ticks. Used to compute deltas between ticks to detect pool health issues.
    
    Stored in Redis and persisted across allocator restarts. Counters are
    cumulative (monotonically increasing) until the proxy restarts or the
    miner reconnects.
    
    Attributes:
        accepted: Total accepted shares (cumulative)
        rejected: Total rejected shares (cumulative)
        rejected_stale: Stale shares (cumulative)
        rejected_duplicate: Duplicate shares (cumulative)
        rejected_low_diff: Low difficulty shares (cumulative)
        rejected_other: Unknown/other rejects (cumulative)
        pool_label: Current pool label (order_id or default label)
    """

    accepted: int = 0
    rejected: int = 0
    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0
    pool_label: str = "unknown"

    @classmethod
    def from_snapshot(cls, miner_snapshot: MinerSnapshot) -> "MinerCounterRecord":
        """Create a counter record from a miner snapshot.
    
        Extracts share statistics from a miner snapshot for use in pool health
        evaluation. Used to build current counters that will be compared with
        previous counters to compute deltas.
        
        Args:
            miner_snapshot: Miner snapshot with current share counts
        
        Returns:
            MinerCounterRecord with counters from the snapshot
        """
        return cls(
            accepted=int(miner_snapshot.accepted or 0),
            rejected=int(miner_snapshot.rejected or 0),
            rejected_stale=int(miner_snapshot.rejected_stale or 0),
            rejected_duplicate=int(miner_snapshot.rejected_duplicate or 0),
            rejected_low_diff=int(miner_snapshot.rejected_low_diff or 0),
            rejected_other=int(miner_snapshot.rejected_other or 0),
            pool_label=str(miner_snapshot.pool_label or "unknown"),
        )

    def as_dict(self) -> dict:
        """Convert counter record to a dictionary.
    
        Used for serialization to Redis. All fields are included as-is.
        
        Returns:
            Dictionary representation of the counter record
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MinerCounterRecord":
        """Create a counter record from a dictionary.
    
        Used for deserialization from Redis. Handles missing fields by defaulting
        to 0 or "unknown".
        
        Args:
            data: Dictionary containing counter record fields
        
        Returns:
            MinerCounterRecord with values from the dictionary
        """
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
    warmup_until_ts: float = 0.0  # When warmup ends
    backoff_until_ts: float = 0.0  # When backoff expires

    # Backoff exponent for repeated failures.
    backoff_level: int = 0  # 0-10, 10 is max

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


def _aggregate_per_order_ticks(
    *,
    miners: list[MinerSnapshot],
    prev_miner_counters: dict[str, MinerCounterRecord],
    default_labels: set[str],
) -> tuple[dict[str, OrderHealthTick], dict[str, MinerCounterRecord]]:
    """
    Build per-order aggregated deltas from miner cumulative counters.

    Behavior preserved:
      - If miner has no history OR moved pools since last tick: do NOT attribute deltas.
      - If counters decreased (reset/restart): do NOT attribute deltas.
      - Always track min_last_notify_age_s for the order if we saw a miner on it.
    """
    per_order: dict[str, OrderHealthTick] = {}
    miner_counters: dict[str, MinerCounterRecord] = {}

    for miner in miners:
        worker_ip = str(miner.worker_ip)
        current = MinerCounterRecord.from_snapshot(miner)
        miner_counters[worker_ip] = current

        label = str(miner.pool_label or "unknown")
        if label in default_labels:
            continue

        tick = per_order.setdefault(label, OrderHealthTick())
        tick.observe_notify_age(miner.last_notify_age_s)

        prev = prev_miner_counters.get(worker_ip)
        if prev is None or str(prev.pool_label) != label:
            # No history or miner moved pools => skip deltas this tick
            continue

        # Counter resets / restarts (proxy restarted / miner reconnected)
        if (
            current.accepted < prev.accepted
            or current.rejected < prev.rejected
            or current.rejected_other < prev.rejected_other
        ):
            continue

        delta_accepted = current.accepted - prev.accepted
        delta_rejected = current.rejected - prev.rejected
        delta_other = current.rejected_other - prev.rejected_other
        delta_total = max(0, delta_accepted + delta_rejected)

        tick.delta_accepted += int(max(0, delta_accepted))
        tick.delta_rejected += int(max(0, delta_rejected))
        tick.delta_other_rejected += int(max(0, delta_other))
        tick.delta_total += int(delta_total)

    return per_order, miner_counters


def _emit_event(
    events: list[dict],
    *,
    level: str,
    code: str,
    order_id: str,
    message: str,
    data: dict,
) -> None:
    # Keep schema identical to your original events.
    events.append(
        {
            "level": level,
            "code": code,
            "order_id": order_id,
            "message": message,
            "data": data,
        }
    )


def _apply_backoff(
    *,
    record: OrderHealthRecord,
    now_ts: float,
    backoff_s: int,
    reason: str,
) -> None:
    """
    Place order in BACKOFF. Behavior preserved:
      - backoff_level increments (capped to 10)
      - warmup cleared ONLY in the 'no jobs' path in original code (caller decides)
    """
    level = max(0, int(record.backoff_level))
    record.status = OrderHealthStatus.BACKOFF
    record.reason = str(reason)
    record.backoff_until_ts = now_ts + float(backoff_s)
    record.backoff_level = min(level + 1, 10)


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
    alpha = _clamp01(float(config.pool_health_ema_alpha))
    default_labels = set(config.default_labels)

    # ------------------------------------------------------------------
    # 1) Aggregate per-order ticks and build current miner counters
    # ------------------------------------------------------------------
    per_order_tick, miner_counters = _aggregate_per_order_ticks(
        miners=miners,
        prev_miner_counters=prev_miner_counters,
        default_labels=default_labels,
    )

    # ------------------------------------------------------------------
    # 2) Update per-order health records
    # ------------------------------------------------------------------
    updated: dict[str, OrderHealthRecord] = dict(
        prev_order_health
    )  # shallow copy (preserve behavior)
    events: list[dict] = []

    relevant_order_ids = set(updated.keys()) | set(per_order_tick.keys())

    for order_id in relevant_order_ids:
        record = updated.get(order_id) or OrderHealthRecord()
        tick = per_order_tick.get(order_id) or OrderHealthTick()

        # ---- Backoff handling ----
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

        # ---- Warmup handling ----
        has_activity = (tick.min_last_notify_age_s is not None) or (
            tick.delta_total > 0
        )

        # Start warmup when we first see activity and we don't already have a warmup window.
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

        # ---- Job-gap EMA (always updated when we have notify ages) ----
        if tick.min_last_notify_age_s is not None:
            record.ema_job_gap_s = _ema_update(
                record.ema_job_gap_s, alpha, float(tick.min_last_notify_age_s)
            )

        # ---- Hard unhealthy signal: no jobs for too long ----
        if tick.min_last_notify_age_s is not None and float(
            tick.min_last_notify_age_s
        ) >= float(config.pool_health_no_notify_seconds):
            backoff_s = _compute_backoff_seconds(record, config)
            _apply_backoff(
                record=record,
                now_ts=now_ts,
                backoff_s=backoff_s,
                reason=f"no_jobs_for_{int(tick.min_last_notify_age_s)}s",
            )
            # Original code clears warmup here:
            record.warmup_until_ts = 0.0

            record.last_update_ts = now_ts
            updated[order_id] = record

            _emit_event(
                events,
                level="warning",
                code="POOL_NO_JOBS",
                order_id=order_id,
                message="Pool appears to be not sending jobs; placing order in backoff",
                data={
                    "min_last_notify_age_s": tick.min_last_notify_age_s,
                    "backoff_seconds": backoff_s,
                    "backoff_level": record.backoff_level,
                },
            )
            continue

        # ---- Shares-based evaluation (skip during warmup) ----
        if record.status != OrderHealthStatus.WARMUP and tick.delta_total >= int(
            config.pool_health_min_samples
        ):
            reject_rate = float(tick.delta_rejected) / float(max(1, tick.delta_total))
            other_rate = float(tick.delta_other_rejected) / float(
                max(1, tick.delta_total)
            )

            record.ema_reject_rate = _ema_update(
                record.ema_reject_rate, alpha, reject_rate
            )
            record.ema_other_reject_rate = _ema_update(
                record.ema_other_reject_rate, alpha, other_rate
            )

            # If unknown/other rejects are consistently high, treat as unhealthy.
            if record.ema_other_reject_rate >= float(
                config.pool_health_other_reject_threshold
            ):
                backoff_s = _compute_backoff_seconds(record, config)
                _apply_backoff(
                    record=record,
                    now_ts=now_ts,
                    backoff_s=backoff_s,
                    reason="high_unknown_rejects",
                )

                _emit_event(
                    events,
                    level="warning",
                    code="POOL_UNHEALTHY_REJECTS",
                    order_id=order_id,
                    message="Pool appears unhealthy (high other rejects); placing order in backoff",
                    data={
                        "delta_total": tick.delta_total,
                        "delta_rejected": tick.delta_rejected,
                        "delta_other_rejected": tick.delta_other_rejected,
                        "ema_other_reject_rate": record.ema_other_reject_rate,
                        "threshold": config.pool_health_other_reject_threshold,
                        "backoff_seconds": backoff_s,
                        "backoff_level": record.backoff_level,
                    },
                )

            elif record.ema_reject_rate >= float(config.pool_health_reject_threshold):
                # Degraded but not drained by default. Useful for dashboards.
                if record.status != OrderHealthStatus.DEGRADED:
                    record.status = OrderHealthStatus.DEGRADED
                    record.reason = "high_reject_rate"

                    _emit_event(
                        events,
                        level="info",
                        code="POOL_DEGRADED",
                        order_id=order_id,
                        message="Pool appears degraded (high reject rate)",
                        data={
                            "delta_total": tick.delta_total,
                            "ema_reject_rate": record.ema_reject_rate,
                            "threshold": config.pool_health_reject_threshold,
                        },
                    )
            else:
                # Recovery
                if record.status == OrderHealthStatus.DEGRADED:
                    record.status = OrderHealthStatus.HEALTHY
                    record.reason = ""

        record.last_update_ts = now_ts
        updated[order_id] = record

    return PoolHealthDecision(
        order_health=updated,
        miner_counters=miner_counters,
        events=events,
    )
