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


class PoolHealthState(str, Enum):
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


def _ema_update(prev_ema: float, damping_factor: float, sample: float) -> float:
    """Update exponential moving average (EMA) with a new sample.

    Computes the next EMA value using the formula:
    EMA_new = (1 - damping_factor) * EMA_prev + damping_factor * sample

    Used to smooth reject rates and job gaps over time, preventing false
    positives from temporary spikes.

    Args:
        prev_ema: Previous EMA value (0.0 if first sample)
        damping_factor: Smoothing factor (0-1), higher = more responsive to changes
        sample: New sample value

    Returns:
        Updated EMA value. Returns the sample if prev_ema <= 0 (first sample).
    """
    if prev_ema <= 0:
        return float(sample)
    return (1.0 - float(damping_factor)) * float(prev_ema) + float(damping_factor) * float(sample)


def _compute_backoff_seconds(
    record: "PoolHealthRecord", config: AllocatorConfig
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
class MinerPoolShareStats:
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
    def from_snapshot(cls, miner_snapshot: MinerSnapshot) -> "MinerPoolShareStats":
        """Create a counter record from a miner snapshot.

        Extracts share statistics from a miner snapshot for use in pool health
        evaluation. Used to build current counters that will be compared with
        previous counters to compute deltas.

        Args:
            miner_snapshot: Miner snapshot with current share counts

        Returns:
            MinerPoolShareStats with counters from the snapshot
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
    def from_dict(cls, data: dict) -> "MinerPoolShareStats":
        """Create a counter record from a dictionary.

        Used for deserialization from Redis. Handles missing fields by defaulting
        to 0 or "unknown".

        Args:
            data: Dictionary containing counter record fields

        Returns:
            MinerPoolShareStats with values from the dictionary
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
class PoolHealthRecord:
    """Persistent per-order health state for upstream pool monitoring.

    Tracks the health status and metrics of an upstream mining pool. Used by
    the allocator to determine whether an order can receive hashrate. Orders
    in BACKOFF are temporarily excluded from allocation until backoff expires.

    Stored in Redis and persisted across allocator restarts. Health state
    transitions through: HEALTHY -> WARMUP -> HEALTHY/DEGRADED/BACKOFF.

    Attributes:
        status: Current health status (HEALTHY/WARMUP/DEGRADED/BACKOFF)
        reason: Human-readable reason for the current status
        warmup_until_ts: Unix timestamp when warmup period ends (0 = no warmup)
        backoff_until_ts: Unix timestamp when backoff period expires (0 = no backoff)
        backoff_level: Exponential backoff level (0-10, increments on repeated failures)
        ema_reject_rate: Smoothed reject rate (0-1) via exponential moving average
        ema_other_reject_rate: Smoothed unknown/other reject rate (0-1)
        ema_job_gap_s: Smoothed job gap in seconds (time since last job notification)
        last_update_ts: Unix timestamp of last health evaluation
    """

    status: PoolHealthState = PoolHealthState.HEALTHY
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
    def from_dict(cls, data: dict) -> "PoolHealthRecord":
        """Create a health record from a dictionary loaded from Redis.

        Args:
            data: Dictionary containing health record fields

        Returns:
            PoolHealthRecord with values from the dictionary
        """
        status_raw = str(data.get("status") or PoolHealthState.HEALTHY.value)
        try:
            status = PoolHealthState(status_raw)
        except Exception:
            status = PoolHealthState.HEALTHY
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
class PoolHealthDelta:
    """One-tick aggregated share deltas for an order.

    Aggregates share statistics across all miners on an order for a single
    tick. Used to compute reject rates and detect pool health issues.

    Attributes:
        delta_accepted: Accepted shares this tick (delta from previous counters)
        delta_rejected: Rejected shares this tick (delta from previous counters)
        delta_other_rejected: Unknown/other rejected shares this tick
        delta_total: Total shares this tick (accepted + rejected)
        min_last_notify_age_s: Smallest last_notify_age_s across miners on the order.
                              None if no miners have this metric. Used to detect
                              pools that stop sending jobs.
    """

    delta_accepted: int = 0
    delta_rejected: int = 0
    delta_other_rejected: int = 0
    delta_total: int = 0

    min_last_notify_age_s: Optional[float] = None

    def observe_notify_age(self, age: Optional[float]) -> None:
        """Update the minimum job notification age for this order.

        Tracks the smallest last_notify_age_s across all miners on the order.
        Used to detect when a pool stops sending jobs (all miners have high
        notify age).

        Args:
            age: Time since last job notification (seconds), or None if unavailable.
                If None, the minimum is unchanged.
        """
        if age is None:
            return
        if self.min_last_notify_age_s is None:
            self.min_last_notify_age_s = float(age)
        else:
            self.min_last_notify_age_s = min(self.min_last_notify_age_s, float(age))


@dataclass
class PoolHealthDecision:
    """Result of pool health evaluation for a single tick.

    Contains updated health records, miner counters, and diagnostic events.
    Returned by evaluate_pool_health() and persisted to Redis for use in
    the next tick and by the rules pipeline.

    Attributes:
        order_health: Updated health records per order (order_id -> PoolHealthRecord)
        miner_share_stats: Updated counter records per miner (miner_id -> MinerPoolShareStats)
        events: List of diagnostic events (warnings/info) for telemetry
    """

    order_health: dict[str, PoolHealthRecord]
    miner_share_stats: dict[str, MinerPoolShareStats]
    events: list[dict]


def _aggregate_per_order_ticks(
    *,
    miners: list[MinerSnapshot],
    prev_miner_share_stats: dict[str, MinerPoolShareStats],
    default_labels: set[str],
) -> tuple[dict[str, PoolHealthDelta], dict[str, MinerPoolShareStats]]:
    """Aggregate per-order share deltas from miner counters.

    Computes share deltas (accepted/rejected) for each order by comparing
    current miner counters with previous counters. Only attributes deltas
    when miners have consistent history (same pool, no counter resets).

    Behavior:
        - Skips deltas if miner has no history or moved pools since last tick
        - Skips deltas if counters decreased (proxy restart or miner reconnect)
        - Always tracks min_last_notify_age_s for orders (even without deltas)

    Args:
        miners: Current miner snapshots with share counts
        prev_miner_share_stats: Previous tick's counter records (from Redis)
        default_labels: Labels that are not marketplace orders (e.g., "normal")

    Returns:
        Tuple of:
            - per_order: Dict mapping order_id -> PoolHealthDelta with aggregated deltas
            - miner_share_stats: Dict mapping miner_id -> MinerPoolShareStats (current counters)
    """
    per_order: dict[str, PoolHealthDelta] = {}
    miner_share_stats: dict[str, MinerPoolShareStats] = {}

    for miner in miners:
        miner_id = str(miner.miner_id)
        current = MinerPoolShareStats.from_snapshot(miner)
        miner_share_stats[miner_id] = current

        label = str(miner.pool_label or "unknown")
        if label in default_labels:
            continue

        tick = per_order.setdefault(label, PoolHealthDelta())
        tick.observe_notify_age(miner.last_notify_age_s)

        prev = prev_miner_share_stats.get(miner_id)
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

    return per_order, miner_share_stats


def _emit_event(
    events: list[dict],
    *,
    level: str,
    code: str,
    order_id: str,
    message: str,
    data: dict,
) -> None:
    """Emit a diagnostic event for telemetry.

    Adds an event to the events list for monitoring and debugging. Events
    are included in TickDiagnostics and published to Redis for real-time
    monitoring.

    Args:
        events: List of events to append to
        level: Event severity ("info", "warning", "error")
        code: Event code (e.g., "POOL_NO_JOBS", "POOL_DEGRADED")
        order_id: Order ID this event relates to
        message: Human-readable message
        data: Additional event data (metrics, thresholds, etc.)
    """
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
    record: PoolHealthRecord,
    now_ts: float,
    backoff_s: int,
    reason: str,
) -> None:
    """Place an order in BACKOFF state due to pool health issues.

    Sets the order status to BACKOFF and increments the backoff level for
    exponential backoff. The order will be excluded from allocation until
    backoff_until_ts expires.

    Args:
        record: Health record to update (modified in place)
        now_ts: Current Unix timestamp
        backoff_s: Backoff duration in seconds (computed via _compute_backoff_seconds)
        reason: Human-readable reason for the backoff

    Note:
        Backoff level increments (capped at 10) to increase wait time after
        repeated failures. Warmup is NOT cleared here (caller must clear if needed).
    """
    level = max(0, int(record.backoff_level))
    record.status = PoolHealthState.BACKOFF
    record.reason = str(reason)
    record.backoff_until_ts = now_ts + float(backoff_s)
    record.backoff_level = min(level + 1, 10)


def evaluate_pool_health(
    *,
    now: datetime,
    miners: list[MinerSnapshot],
    prev_order_health: dict[str, PoolHealthRecord],
    prev_miner_share_stats: dict[str, MinerPoolShareStats],
    config: AllocatorConfig,
) -> PoolHealthDecision:
    """Evaluate upstream pool health and update health records.

    Analyzes miner share statistics to detect pool health issues. Uses
    exponential moving averages (EMA) to smooth metrics and prevent false
    positives. Applies warmup windows and exponential backoff for hysteresis.

    Health evaluation steps:
        1. Aggregate per-order share deltas from miner counters
        2. Update health records with state transitions:
           - BACKOFF: Check if expired, clear if so
           - WARMUP: Start warmup on first activity, complete after window
           - Job gap: Update EMA, trigger backoff if no jobs for too long
           - Reject rates: Update EMAs, trigger DEGRADED/BACKOFF if thresholds exceeded

    Args:
        now: Current timestamp for health evaluation
        miners: Current miner snapshots with share counts and pool assignments
        prev_order_health: Previous tick's health records (from Redis)
        prev_miner_share_stats: Previous tick's miner counters (from Redis)
        config: Allocator configuration with health thresholds and settings

    Returns:
        PoolHealthDecision with updated health records, miner counters, and events.
        If pool health is disabled, returns previous health unchanged.
    """
    if not config.enable_pool_health:
        miner_share_stats = {
            m.miner_id: MinerPoolShareStats.from_snapshot(m) for m in miners
        }
        return PoolHealthDecision(
            order_health=dict(prev_order_health),
            miner_share_stats=miner_share_stats,
            events=[],
        )

    now_ts = _utc_now_ts(now)
    damping_factor = _clamp01(float(config.pool_health_ema_alpha))
    default_labels = set(config.default_labels)

    # ------------------------------------------------------------------
    # 1) Aggregate per-order ticks and build current miner counters
    # ------------------------------------------------------------------
    per_order_tick, miner_share_stats = _aggregate_per_order_ticks(
        miners=miners,
        prev_miner_share_stats=prev_miner_share_stats,
        default_labels=default_labels,
    )

    # ------------------------------------------------------------------
    # 2) Update per-order health records
    # ------------------------------------------------------------------
    updated: dict[str, PoolHealthRecord] = dict(prev_order_health)
    events: list[dict] = []

    relevant_order_ids = set(updated.keys()) | set(per_order_tick.keys())

    for order_id in relevant_order_ids:
        record = updated.get(order_id) or PoolHealthRecord()
        tick = per_order_tick.get(order_id) or PoolHealthDelta()

        # Track whether we *just* exited backoff during this tick.
        #
        # This matters for warmup behavior: when an order returns from BACKOFF
        # we want a warmup window to allow the upstream pool to stabilize.
        #
        # IMPORTANT: we intentionally do *not* start warmup on every tick with
        # activity; otherwise an order with continuous hashrate would repeatedly
        # re-enter WARMUP and never be evaluated by share-based heuristics.
        backoff_just_expired = False

        # ---- Backoff handling ----
        if record.status == PoolHealthState.BACKOFF and record.backoff_until_ts > 0:
            if now_ts < record.backoff_until_ts:
                record.last_update_ts = now_ts
                updated[order_id] = record
                continue

            # Backoff expired; clear and allow allocation again.
            record.status = PoolHealthState.HEALTHY
            record.reason = ""
            record.backoff_until_ts = 0.0
            record.warmup_until_ts = 0.0
            # Keep backoff_level so repeated failures escalate.
            backoff_just_expired = True

        # ---- Warmup handling ----
        has_activity = (tick.min_last_notify_age_s is not None) or (
            tick.delta_total > 0
        )

        # Start warmup:
        #   - for *new* orders (first time we see the order_id), or
        #   - when an order returns from BACKOFF.
        #
        # We purposely avoid starting warmup for orders that are already
        # HEALTHY and receiving continuous hashpower, otherwise the warmup
        # window would restart every time it expires.
        #
        # NOTE: we detect "new order" via the presence of order_id in
        # prev_order_health (not via record.last_update_ts) because older
        # persisted records may not carry last_update_ts across restarts.
        is_new_order = order_id not in prev_order_health
        if has_activity and record.warmup_until_ts <= 0 and (
            is_new_order or backoff_just_expired
        ):
            record.status = PoolHealthState.WARMUP
            record.warmup_until_ts = now_ts + float(config.pool_health_warmup_seconds)
            record.reason = "warmup"

        # Warmup completion
        if record.status == PoolHealthState.WARMUP:
            if record.warmup_until_ts > 0 and now_ts >= record.warmup_until_ts:
                record.status = PoolHealthState.HEALTHY
                record.reason = ""
                record.warmup_until_ts = 0.0

        # ---- Job-gap EMA (always updated when we have notify ages) ----
        if tick.min_last_notify_age_s is not None:
            record.ema_job_gap_s = _ema_update(
                record.ema_job_gap_s, damping_factor, float(tick.min_last_notify_age_s)
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
        if record.status != PoolHealthState.WARMUP and tick.delta_total >= int(
            config.pool_health_min_samples
        ):
            reject_rate = float(tick.delta_rejected) / float(max(1, tick.delta_total))
            other_rate = float(tick.delta_other_rejected) / float(
                max(1, tick.delta_total)
            )

            record.ema_reject_rate = _ema_update(
                record.ema_reject_rate, damping_factor, reject_rate
            )
            record.ema_other_reject_rate = _ema_update(
                record.ema_other_reject_rate, damping_factor, other_rate
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
                # Degraded but not drained by default.
                if record.status != PoolHealthState.DEGRADED:
                    record.status = PoolHealthState.DEGRADED
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
                if record.status == PoolHealthState.DEGRADED:
                    record.status = PoolHealthState.HEALTHY
                    record.reason = ""

        record.last_update_ts = now_ts
        updated[order_id] = record

    return PoolHealthDecision(
        order_health=updated,
        miner_share_stats=miner_share_stats,
        events=events,
    )
