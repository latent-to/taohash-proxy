"""
This module provides an aggregation interface for stats across multiple
miner sessions into a single logical miner group for Redis heartbeat publishing.

Also handles cross-proxy merging when the same worker_ip appears on multiple
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
