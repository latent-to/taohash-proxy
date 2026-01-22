"""
This module provides an aggregation interface for stats across multiple
miner sessions into a single logical miner group for Redis heartbeat publishing.

Also handles cross-proxy merging when the same miner_id appears on multiple
proxy instances.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


def _to_float(x: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _to_int(x: Any, default: int = 0) -> int:
    """Safely convert value to int."""
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


@dataclass
class MinerGroupStats:
    """Aggregated stats across all sessions for a logical miner group.

    A miner group is identified by a registry key (worker_name:ip) and may
    contain multiple sessions when miners are behind NAT or reuse worker names.
    """

    hashrate: float = 0.0
    difficulty: float = 0.0
    accepted: int = 0
    rejected: int = 0
    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0
    pool_disconnects: int = 0
    connected_minutes: float = 0.0
    pool_labels: list[str] = field(default_factory=list)
    last_notify_ages: list[float] = field(default_factory=list)
    session_rows: list[dict] = field(default_factory=list)

    def add_session(self, sess: Any, now_dt: datetime, now_ts: float) -> None:
        """Aggregate stats from a single miner session.

        Args:
            sess: A MinerSession object with stats attribute.
            now_dt: Current datetime for connected_minutes calculation.
            now_ts: Current unix timestamp for last_notify_age calculation.
        """
        session_stats = getattr(sess, "stats", None)
        if not session_stats:
            return

        # Hashrate
        try:
            hashrate = float(session_stats.get_hashrate() or 0.0)
        except Exception:
            hashrate = 0.0
        self.hashrate += hashrate

        # Difficulty (take max across sessions)
        try:
            self.difficulty = max(
                self.difficulty, float(session_stats.difficulty or 0.0)
            )
        except Exception:
            pass

        # Share counters
        self.accepted += int(getattr(session_stats, "accepted", 0) or 0)
        self.rejected += int(getattr(session_stats, "rejected", 0) or 0)
        self.rejected_stale += int(getattr(session_stats, "rejected_stale", 0) or 0)
        self.rejected_duplicate += int(
            getattr(session_stats, "rejected_duplicate", 0) or 0
        )
        self.rejected_low_diff += int(
            getattr(session_stats, "rejected_low_diff", 0) or 0
        )
        self.rejected_other += int(getattr(session_stats, "rejected_other", 0) or 0)
        self.pool_disconnects += int(getattr(session_stats, "pool_disconnects", 0) or 0)

        # Connected time (take max across sessions)
        try:
            connected_mins = (
                now_dt - session_stats.connected_at
            ).total_seconds() / 60.0
        except Exception:
            connected_mins = 0.0
        self.connected_minutes = max(self.connected_minutes, connected_mins)

        # Pool label for majority vote
        self.pool_labels.append(str(sess.current_pool.label))

        # Pool job liveness signal
        last_notify_time = float(getattr(session_stats, "last_notify_time", 0.0) or 0.0)
        if last_notify_time > 0:
            self.last_notify_ages.append(max(0.0, now_ts - last_notify_time))

        # Session row for detailed breakdown
        self.session_rows.append(
            {
                "session_id": sess.get_session_id(),
                "miner_id": sess.miner_id,
                "hashrate_gh": hashrate / 1e9,
                "accepted": int(getattr(session_stats, "accepted", 0) or 0),
                "rejected": int(getattr(session_stats, "rejected", 0) or 0),
                "pool_label": str(sess.current_pool.label),
                "connected_minutes": round(connected_mins, 1),
            }
        )

    def get_pool_label(self) -> str:
        """Determine pool label by majority.

        Returns 'unknown' if there's a tie or no labels recorded.
        """
        if not self.pool_labels:
            return "unknown"

        counts = Counter(self.pool_labels)
        top_label, top_count = counts.most_common(1)[0]

        tied = [label for label, count in counts.items() if count == top_count]
        if len(tied) == 1:
            return top_label

        return "unknown"

    def to_dict(self, sessions: list[Any]) -> dict:
        """Convert to the dict format expected by the allocator.

        Args:
            sessions: List of original session objects (for worker_name/miner_agent).

        Returns:
            Dict matching the schema used by update_proxy_miners_with_stats().
        """
        return {
            "hashrate": self.hashrate,
            "hashrate_gh": self.hashrate / 1e9,
            "difficulty": self.difficulty,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejected_breakdown": {
                "stale": self.rejected_stale,
                "duplicate": self.rejected_duplicate,
                "low_diff": self.rejected_low_diff,
                "other": self.rejected_other,
            },
            "pool_disconnects": self.pool_disconnects,
            "pool_label": self.get_pool_label(),
            "worker_name": sessions[0].worker_name or "" if sessions else "",
            "miner_agent": (
                sessions[0].stats.miner_agent if sessions and sessions[0].stats else ""
            ),
            "connected_minutes": round(self.connected_minutes, 1),
            "last_notify_age_s": (
                min(self.last_notify_ages) if self.last_notify_ages else None
            ),
            "session_count": len(sessions),
            "sessions": self.session_rows,
        }

    def merge_from_dict(
        self,
        stats: dict,
        worker_name_out: Optional[list] = None,
        miner_agent_out: Optional[list] = None,
    ) -> None:
        """Merge stats from a dict representation into this instance.

        Used for cross-proxy aggregation when the same miner_id appears on
        multiple proxies.

        Args:
            stats: Dict from Redis (output of to_dict or similar).
            worker_name_out: Optional list to collect worker_name values.
            miner_agent_out: Optional list to collect miner_agent values.
        """
        # Sum primary counters
        self.hashrate += _to_float(stats.get("hashrate"))
        self.accepted += _to_int(stats.get("accepted"))
        self.rejected += _to_int(stats.get("rejected"))
        self.pool_disconnects += _to_int(stats.get("pool_disconnects"))

        # Max for difficulty and connected_minutes
        self.difficulty = max(self.difficulty, _to_float(stats.get("difficulty")))
        self.connected_minutes = max(
            self.connected_minutes,
            _to_float(stats.get("connected_minutes")),
        )

        # Merge rejected breakdown
        breakdown = stats.get("rejected_breakdown") or {}
        self.rejected_stale += _to_int(breakdown.get("stale"))
        self.rejected_duplicate += _to_int(breakdown.get("duplicate"))
        self.rejected_low_diff += _to_int(breakdown.get("low_diff"))
        self.rejected_other += _to_int(breakdown.get("other"))

        # last_notify_age_s: smaller is better (more recent). Collect for min.
        last_notify_age = stats.get("last_notify_age_s")
        if last_notify_age is not None:
            self.last_notify_ages.append(_to_float(last_notify_age, 1e18))

        # Concatenate sessions
        src_sessions = stats.get("sessions") or []
        self.session_rows.extend(src_sessions)

        # Collect pool labels from sessions for majority vote
        for session in src_sessions:
            if isinstance(session, dict) and session.get("pool_label"):
                self.pool_labels.append(str(session.get("pool_label")))

        # If no session-level labels, use top-level pool_label
        if not src_sessions and stats.get("pool_label"):
            self.pool_labels.append(str(stats.get("pool_label")))

        # Collect worker_name and miner_agent for first-non-empty logic
        if worker_name_out is not None and stats.get("worker_name"):
            worker_name_out.append(stats.get("worker_name"))
        if miner_agent_out is not None and stats.get("miner_agent"):
            miner_agent_out.append(stats.get("miner_agent"))

    def to_merged_dict(
        self,
        worker_name: str = "",
        miner_agent: str = "",
    ) -> dict:
        """Convert to dict after cross-proxy merging.

        Unlike to_dict(), this doesn't require session objects since we're
        working with already-serialized data from Redis.

        Args:
            worker_name: First non-empty worker_name collected during merging.
            miner_agent: First non-empty miner_agent collected during merging.

        Returns:
            Dict matching the schema for the allocator.
        """
        return {
            "hashrate": self.hashrate,
            "hashrate_gh": self.hashrate / 1e9,
            "difficulty": self.difficulty,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejected_breakdown": {
                "stale": self.rejected_stale,
                "duplicate": self.rejected_duplicate,
                "low_diff": self.rejected_low_diff,
                "other": self.rejected_other,
            },
            "pool_disconnects": self.pool_disconnects,
            "pool_label": self.get_pool_label(),
            "worker_name": worker_name,
            "miner_agent": miner_agent,
            "connected_minutes": round(self.connected_minutes, 1),
            "last_notify_age_s": (
                min(self.last_notify_ages) if self.last_notify_ages else None
            ),
            "session_count": len(self.session_rows),
            "sessions": self.session_rows,
        }
