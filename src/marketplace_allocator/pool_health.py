"""
TaoHash Marketplace Allocator Pool Health Evaluation.

This module evaluates the health of upstream mining pools by analyzing miner
share statistics (accepted/rejected counts) and job notification patterns.

Key Features:
  * Monitors pool health via proxy heartbeats (share counts, reject breakdown,
    last_notify_age_s) to detect when upstream pools are failing
  * Applies hysteresis via EMA smoothing, warmup windows, and exponential backoff
  * Produces per-order health status used by the allocator to exclude unhealthy orders

Health States:
  * HEALTHY: Pool functioning normally, can receive hashrate
  * WARMUP: Just started receiving hashrate, waiting for initial samples
  * DEGRADED: High reject rate but still receiving hashrate
  * BACKOFF: Unhealthy (no jobs, high unknown rejects), temporarily excluded
  * PROBATION: Careful re-entry after backoff, limited hashrate allowed
  * FAILED: Permanent failure after max backoffs, excluded indefinitely

Used by:
  * Allocator loop: Evaluates health before each planning cycle
  * Rules pipeline: PoolHealthRule uses BACKOFF/FAILED status to exclude orders
  * Planner: Hard-drains orders in BACKOFF state
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.marketplace_allocator.types import AllocatorConfig, MinerSnapshot


class PoolHealthStatus(str, Enum):
    """Health status of an upstream mining pool.

    Used to determine whether an order can receive hashrate. Orders in BACKOFF
    are temporarily excluded from allocation until the backoff period expires.

    Values:
        HEALTHY: Pool is functioning normally and can receive hashrate
        WARMUP: Pool just started receiving hashrate (waiting for initial samples)
        DEGRADED: Pool has high reject rate but still receiving hashrate
        BACKOFF: Pool is unhealthy (no jobs, high unknown rejects) - miners drained
        PROBATION: Careful re-entry after backoff, limited hashrate allowed
        FAILED: Permanent failure after max backoffs - order excluded indefinitely
    """

    HEALTHY = "HEALTHY"
    WARMUP = "WARMUP"
    DEGRADED = "DEGRADED"
    BACKOFF = "BACKOFF"
    PROBATION = "PROBATION"
    FAILED = "FAILED"


PoolHealthState = PoolHealthStatus


class SignalType(str, Enum):
    """Classified signal type for state machine transitions.

    Represents the discrete signal derived from tick data that drives
    state transitions. Signals are prioritized: hard signals (NO_JOBS,
    HIGH_OTHER_REJECTS) override soft signals.
    """

    GOOD = "GOOD"  # Healthy metrics, all thresholds passed
    NO_DATA = "NO_DATA"  # Not enough samples this tick
    NO_JOBS = "NO_JOBS"  # Pool not sending jobs (hard signal)
    HIGH_REJECTS = "HIGH_REJECTS"  # High overall reject rate
    HIGH_OTHER_REJECTS = "HIGH_OTHER_REJECTS"  # High unknown rejects (hard signal)
    TIMER_EXPIRED = "TIMER_EXPIRED"  # Warmup or backoff timer completed


@dataclass(frozen=True)
class HealthSignal:
    """Classified input signal for state machine transition.

    Produced by classify_signal() from tick data. Contains both the
    discrete signal type and the raw metrics for diagnostics/logging.
    """

    signal_type: SignalType
    reject_rate: float = 0.0
    other_reject_rate: float = 0.0
    job_gap_s: float = 0.0
    sample_count: int = 0


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
    """Clamp a float value to the range [0, 1].

    Used to ensure EMA damping factors and rate values stay within valid bounds.

    Args:
        x: Float value to clamp

    Returns:
        Value clamped to [0.0, 1.0]
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
    return (1.0 - float(damping_factor)) * float(prev_ema) + float(
        damping_factor
    ) * float(sample)


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
    base_backoff_seconds = int(config.pool_health_backoff_base_seconds)
    max_backoff_seconds = int(config.pool_health_backoff_max_seconds)
    level = max(0, int(record.backoff_level))
    return int(min(max_backoff_seconds, base_backoff_seconds * (2**level)))


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
    transitions through: HEALTHY -> WARMUP -> HEALTHY/DEGRADED/BACKOFF/PROBATION/FAILED.

    Attributes:
        status: Current health status (HEALTHY/WARMUP/DEGRADED/BACKOFF/PROBATION/FAILED)
        reason: Human-readable reason for the current status
        warmup_until_ts: Unix timestamp when warmup period ends (0 = no warmup)
        backoff_until_ts: Unix timestamp when backoff period expires (0 = no backoff)
        backoff_level: Exponential backoff level (0-10, increments on repeated failures)
        good_tick_count: Consecutive good ticks (for recovery hysteresis)
        ema_reject_rate: Smoothed reject rate (0-1) via exponential moving average
        ema_other_reject_rate: Smoothed unknown/other reject rate (0-1)
        ema_job_gap_s: Smoothed job gap in seconds (time since last job notification)
        last_update_ts: Unix timestamp of last health evaluation
    """

    status: PoolHealthStatus = PoolHealthStatus.HEALTHY
    reason: str = ""

    # Unix timestamps (seconds). 0 means unset.
    warmup_until_ts: float = 0.0  # When warmup ends
    backoff_until_ts: float = 0.0  # When backoff expires

    # Backoff exponent for repeated failures.
    backoff_level: int = 0  # 0-10, 10 is max

    # Recovery hysteresis counter
    good_tick_count: int = 0

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
        status_raw = str(data.get("status") or PoolHealthStatus.HEALTHY.value)
        try:
            status = PoolHealthStatus(status_raw)
        except Exception:
            status = PoolHealthStatus.HEALTHY
        return cls(
            status=status,
            reason=str(data.get("reason") or ""),
            warmup_until_ts=float(data.get("warmup_until_ts") or 0.0),
            backoff_until_ts=float(data.get("backoff_until_ts") or 0.0),
            backoff_level=int(data.get("backoff_level") or 0),
            good_tick_count=int(data.get("good_tick_count") or 0),
            ema_reject_rate=float(data.get("ema_reject_rate") or 0.0),
            ema_other_reject_rate=float(data.get("ema_other_reject_rate") or 0.0),
            ema_job_gap_s=float(data.get("ema_job_gap_s") or 0.0),
            last_update_ts=float(data.get("last_update_ts") or 0.0),
        )


# Constants for state machine
MAX_BACKOFF_LEVEL = 5  # After this many backoffs, transition to FAILED
RECOVERY_TICKS_REQUIRED = 3  # Consecutive good ticks needed to recover


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


def classify_signal(
    tick: PoolHealthDelta,
    record: PoolHealthRecord,
    config: AllocatorConfig,
    now_ts: float,
) -> HealthSignal:
    """Classify tick data into a discrete signal type for state transitions.

    This function examines the current tick's metrics and the record's
    EMA values to produce a single HealthSignal that drives the state machine.

    IMPORTANT: _update_record_emas() must be called BEFORE this function so that
    record.ema_* values are up-to-date. This ensures we use smoothed (EMA) values
    for reject rate comparisons, providing hysteresis against single-tick spikes.

    Signal priority (highest to lowest):

      1) Hard signals
         - NO_JOBS: pool is not sending jobs (uses instantaneous job-gap signal)
         - HIGH_OTHER_REJECTS: unknown/other rejects above threshold (EMA + min samples)

      2) Timer-driven transitions
         - TIMER_EXPIRED: warmup/backoff deadline reached (inclusive boundary)

      3) Soft/diagnostic signals
         - NO_DATA: not enough samples this tick to evaluate reject-rate thresholds
         - HIGH_REJECTS: overall reject rate above threshold (EMA + min samples)
         - GOOD: all thresholds passed

    Rationale:
      - Hard signals must win to avoid "immunity windows" where a pool transitions
        out of BACKOFF/WARMUP even though it is still clearly unhealthy.
      - Timer expiry is evaluated before NO_DATA so that WARMUP/BACKOFF cannot
        get stuck waiting for samples.

    Args:
        tick: Aggregated share deltas for this order this tick.
        record: Current health record with EMA values (must be updated first).
        config: Allocator config with thresholds.
        now_ts: Current Unix timestamp.

    Returns:
        HealthSignal with classified type and metrics for diagnostics.
    """

    # 1) Hard signal: pool not sending jobs (independent of share samples).
    if tick.min_last_notify_age_s is not None:
        if tick.min_last_notify_age_s >= float(config.pool_health_no_notify_seconds):
            return HealthSignal(
                SignalType.NO_JOBS,
                job_gap_s=float(tick.min_last_notify_age_s),
            )

    # 2) Hard signal: high "other/unknown" reject rate (requires samples).
    min_samples = int(config.pool_health_min_samples)
    if tick.delta_total >= min_samples:
        ema_other_rate = record.ema_other_reject_rate
        ema_reject_rate = record.ema_reject_rate
        if ema_other_rate >= float(config.pool_health_other_reject_threshold):
            return HealthSignal(
                SignalType.HIGH_OTHER_REJECTS,
                reject_rate=float(ema_reject_rate),
                other_reject_rate=float(ema_other_rate),
                sample_count=int(tick.delta_total),
            )

    # 3) Timer transitions (soft), evaluated after hard signals.
    # Missing/invalid timestamps (<=0) as expired to prevent stuck states.
    if record.status == PoolHealthStatus.WARMUP:
        if record.warmup_until_ts <= 0 or now_ts >= record.warmup_until_ts:
            return HealthSignal(SignalType.TIMER_EXPIRED)

    if record.status == PoolHealthStatus.BACKOFF:
        if record.backoff_until_ts <= 0 or now_ts >= record.backoff_until_ts:
            return HealthSignal(SignalType.TIMER_EXPIRED)

    # 4) Not enough samples to evaluate reject-rate thresholds.
    if tick.delta_total < min_samples:
        return HealthSignal(
            SignalType.NO_DATA,
            sample_count=int(tick.delta_total),
        )

    # 5) Soft signal: high overall reject rate (EMA + min samples).
    ema_reject_rate = record.ema_reject_rate
    ema_other_rate = record.ema_other_reject_rate
    if ema_reject_rate >= float(config.pool_health_reject_threshold):
        return HealthSignal(
            SignalType.HIGH_REJECTS,
            reject_rate=float(ema_reject_rate),
            other_reject_rate=float(ema_other_rate),
            sample_count=int(tick.delta_total),
        )

    return HealthSignal(
        SignalType.GOOD,
        reject_rate=float(ema_reject_rate),
        other_reject_rate=float(ema_other_rate),
        sample_count=int(tick.delta_total),
    )


def compute_transition(
    record: PoolHealthRecord,
    signal: HealthSignal,
    config: AllocatorConfig,
    now_ts: float,
) -> tuple[PoolHealthRecord, Optional[str]]:
    """Compute the next state from current state and classified signal.

    This pure function implements the state machine transition logic. It takes
    the current record and signal, and returns a new record with updated state
    plus an optional event code for logging.

    Transition rules:
        - FAILED is terminal (no transitions out)
        - Hard signals (NO_JOBS, HIGH_OTHER_REJECTS) trigger BACKOFF from any state
        - After MAX_BACKOFF_LEVEL backoffs, transition to FAILED
        - Recovery from DEGRADED/PROBATION requires RECOVERY_TICKS_REQUIRED good ticks
        - BACKOFF timer expiry goes to PROBATION (not directly to HEALTHY)

    Args:
        record: Current health record
        signal: Classified signal from classify_signal()
        config: Allocator config for backoff calculation
        now_ts: Current Unix timestamp

    Returns:
        Tuple of (new_record, event_code_or_none). Event codes:
            - POOL_FAILED: Transitioned to terminal FAILED state
            - POOL_BACKOFF: Entered or re-entered BACKOFF
            - POOL_DEGRADED: Transitioned to DEGRADED
            - POOL_WARMUP_COMPLETE: Warmup timer expired
            - POOL_PROBATION: Entered probation after backoff
            - POOL_RECOVERED: Successfully recovered to HEALTHY
    """
    # Create mutable copy with updated timestamp
    new = PoolHealthRecord(
        status=record.status,
        reason=record.reason,
        warmup_until_ts=record.warmup_until_ts,
        backoff_until_ts=record.backoff_until_ts,
        backoff_level=record.backoff_level,
        good_tick_count=record.good_tick_count,
        ema_reject_rate=record.ema_reject_rate,
        ema_other_reject_rate=record.ema_other_reject_rate,
        ema_job_gap_s=record.ema_job_gap_s,
        last_update_ts=now_ts,
    )

    # Terminal state - no transitions out
    if record.status == PoolHealthStatus.FAILED:
        return new, None

    # Hard signals override everything (except FAILED)
    if signal.signal_type in (SignalType.NO_JOBS, SignalType.HIGH_OTHER_REJECTS):
        if record.backoff_level >= MAX_BACKOFF_LEVEL:
            new.status = PoolHealthStatus.FAILED
            new.reason = f"max_backoffs_exceeded_{signal.signal_type.value.lower()}"
            return new, "POOL_FAILED"

        # Enter backoff
        backoff_seconds = _compute_backoff_seconds(record, config)
        new.status = PoolHealthStatus.BACKOFF
        new.reason = signal.signal_type.value.lower()
        new.backoff_until_ts = now_ts + float(backoff_seconds)
        new.backoff_level = min(record.backoff_level + 1, 10)
        new.good_tick_count = 0
        new.warmup_until_ts = 0.0
        return new, "POOL_BACKOFF"

    if record.status == PoolHealthStatus.HEALTHY:
        if signal.signal_type == SignalType.HIGH_REJECTS:
            new.status = PoolHealthStatus.DEGRADED
            new.reason = "high_reject_rate"
            new.good_tick_count = 0
            return new, "POOL_DEGRADED"

    elif record.status == PoolHealthStatus.WARMUP:
        if signal.signal_type == SignalType.TIMER_EXPIRED:
            new.status = PoolHealthStatus.HEALTHY
            new.reason = ""
            new.warmup_until_ts = 0.0
            return new, "POOL_WARMUP_COMPLETE"

    elif record.status == PoolHealthStatus.DEGRADED:
        if signal.signal_type == SignalType.GOOD:
            new.good_tick_count = record.good_tick_count + 1
            if new.good_tick_count >= RECOVERY_TICKS_REQUIRED:
                new.status = PoolHealthStatus.HEALTHY
                new.reason = ""
                new.good_tick_count = 0
                return new, "POOL_RECOVERED"
        elif signal.signal_type == SignalType.HIGH_REJECTS:
            # Reset recovery counter on bad tick
            new.good_tick_count = 0

    elif record.status == PoolHealthStatus.BACKOFF:
        if signal.signal_type == SignalType.TIMER_EXPIRED:
            new.status = PoolHealthStatus.PROBATION
            new.reason = "probation_after_backoff"
            new.backoff_until_ts = 0.0
            new.good_tick_count = 0
            return new, "POOL_PROBATION"

    elif record.status == PoolHealthStatus.PROBATION:
        if signal.signal_type == SignalType.GOOD:
            new.good_tick_count = record.good_tick_count + 1
            if new.good_tick_count >= RECOVERY_TICKS_REQUIRED:
                # Full recovery
                new.status = PoolHealthStatus.HEALTHY
                new.reason = ""
                new.backoff_level = 0
                new.good_tick_count = 0
                return new, "POOL_RECOVERED"

        elif signal.signal_type == SignalType.HIGH_REJECTS:
            # High rejects during probation = back to backoff
            backoff_seconds = _compute_backoff_seconds(record, config)
            new.status = PoolHealthStatus.BACKOFF
            new.reason = f"probation_failed_{signal.signal_type.value.lower()}"
            new.backoff_until_ts = now_ts + float(backoff_seconds)
            new.backoff_level = min(record.backoff_level + 1, 10)
            new.good_tick_count = 0
            return new, "POOL_BACKOFF"
        # NO_DATA during probation: Stay in probation, wait for miners to be assigned.
        # This prevents an infinite loop where orders bounce between BACKOFF and PROBATION
        # when the allocator hasn't assigned miners yet.

    return new, None


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


def _event_level(event_code: str) -> str:
    """Determine event severity level from event code.

    Args:
        event_code: Event code from compute_transition()

    Returns:
        Event level: "warning" for failures/backoffs, "info" otherwise
    """
    if event_code in ("POOL_FAILED", "POOL_BACKOFF"):
        return "warning"
    return "info"


def _event_message(event_code: str) -> str:
    """Get human-readable message for event code.

    Args:
        event_code: Event code from compute_transition()

    Returns:
        Human-readable message describing the state transition
    """
    messages = {
        "POOL_FAILED": "Pool has failed too many times; permanently excluded",
        "POOL_BACKOFF": "Pool is unhealthy; placing order in backoff",
        "POOL_DEGRADED": "Pool is degraded (high reject rate)",
        "POOL_WARMUP_COMPLETE": "Pool warmup completed",
        "POOL_PROBATION": "Pool entering probation after backoff",
        "POOL_RECOVERED": "Pool has recovered to healthy state",
    }
    return messages.get(event_code, f"Pool health event: {event_code}")


def _update_record_emas(
    record: PoolHealthRecord,
    tick: PoolHealthDelta,
    damping_factor: float,
    *,
    min_samples: int,
) -> PoolHealthRecord:
    """Update EMA metrics on the record.

    EMA metrics are used for smoothing reject-rate and job-gap signals.

    Always update the *job gap* EMA whenever there is a job-age observation,
    because it is not based on share sample counts.

    To be called before classify_signal() so that signal classification uses
    the updated EMA values.

    Args:
        record: Current health record.
        tick: Tick delta data.
        damping_factor: EMA smoothing factor (0-1).
        min_samples: Minimum samples required to treat a reject-rate observation as valid.

    Returns:
        The same record instance (mutated) with updated EMA fields.
    """
    # Job gap EMA.
    if tick.min_last_notify_age_s is not None:
        record.ema_job_gap_s = _ema_update(
            record.ema_job_gap_s, damping_factor, float(tick.min_last_notify_age_s)
        )

    # Reject-rate EMAs (when we have enough samples).
    if tick.delta_total >= int(min_samples) and tick.delta_total > 0:
        reject_rate = float(tick.delta_rejected) / float(max(1, tick.delta_total))
        other_rate = float(tick.delta_other_rejected) / float(max(1, tick.delta_total))

        record.ema_reject_rate = _ema_update(
            record.ema_reject_rate, damping_factor, reject_rate
        )
        record.ema_other_reject_rate = _ema_update(
            record.ema_other_reject_rate, damping_factor, other_rate
        )

    return record


def evaluate_pool_health(
    *,
    now: datetime,
    miners: list[MinerSnapshot],
    prev_order_health: dict[str, PoolHealthRecord],
    prev_miner_share_stats: dict[str, MinerPoolShareStats],
    config: AllocatorConfig,
) -> PoolHealthDecision:
    """Evaluate upstream pool health and update health records.

    Uses an explicit state machine pattern with:
        - classify_signal(): Classifies tick data into discrete signal types
        - compute_transition(): Computes next state from current state + signal
        - _update_record_emas(): Updates EMA metrics for monitoring

    State machine features:
        - Recovery hysteresis: Requires RECOVERY_TICKS_REQUIRED good ticks
        - Bounded failure: After MAX_BACKOFF_LEVEL backoffs, enters FAILED state
        - Probation: After backoff expires, enters PROBATION before HEALTHY

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
    min_samples = int(config.pool_health_min_samples)
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
    # 2) Update per-order health records using state machine
    # ------------------------------------------------------------------
    updated: dict[str, PoolHealthRecord] = {}
    events: list[dict] = []

    relevant_order_ids = set(prev_order_health.keys()) | set(per_order_tick.keys())

    for order_id in relevant_order_ids:
        record = prev_order_health.get(order_id) or PoolHealthRecord()
        tick = per_order_tick.get(order_id) or PoolHealthDelta()

        # Handle new orders: start in WARMUP if we have activity
        is_new_order = order_id not in prev_order_health
        has_activity = (tick.min_last_notify_age_s is not None) or (
            tick.delta_total > 0
        )

        if is_new_order and has_activity:
            record.status = PoolHealthStatus.WARMUP
            record.warmup_until_ts = now_ts + float(config.pool_health_warmup_seconds)
            record.reason = "warmup"

        # 1. Update EMAs FIRST (so classify_signal uses smoothed values)
        record = _update_record_emas(
            record, tick, damping_factor, min_samples=min_samples
        )

        # 2. Classify the current tick into a signal (uses EMA values)
        signal = classify_signal(tick, record, config, now_ts)

        # 3. Compute state transition
        new_record, event_code = compute_transition(record, signal, config, now_ts)

        # 4. Emit event if state changed
        if event_code:
            _emit_event(
                events,
                level=_event_level(event_code),
                code=event_code,
                order_id=order_id,
                message=_event_message(event_code),
                data={
                    "signal_type": signal.signal_type.value,
                    "reject_rate": signal.reject_rate,
                    "other_reject_rate": signal.other_reject_rate,
                    "sample_count": signal.sample_count,
                    "job_gap_s": signal.job_gap_s,
                    "backoff_level": new_record.backoff_level,
                    "good_tick_count": new_record.good_tick_count,
                },
            )

        updated[order_id] = new_record

    return PoolHealthDecision(
        order_health=updated,
        miner_share_stats=miner_share_stats,
        events=events,
    )
