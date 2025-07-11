"""
Mining statistics tracking module.

This module provides classes for tracking and calculating statistics for miners
connected to the proxy. It maintains data on shares submitted, difficulty levels,
and calculates estimated hashrates based on recent share history.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import json

from ..utils.logger import get_logger

logger = get_logger(__name__)

ERROR_JOB_NOT_FOUND = 21
ERROR_DUPLICATE_SHARE = 22
ERROR_LOW_DIFFICULTY = 23


@dataclass
class MinerStats:
    """
    Statistics tracker for an individual miner connection.

    Stores connection details, share counts, and calculates hashrate based on
    submitted shares over time using the difficulty-adjusted share method.

    Attributes:
        ip (str): Miner's IP address
        worker_name (str): Worker name from mining.authorize
        connected_at (float): Connection timestamp
        accepted (int): Count of accepted shares
        rejected (int): Count of rejected shares
        difficulty (float): Current share difficulty
        recent_shares (deque): Queue of (timestamp, difficulty) tuples for hashrate calculation
        highest_difficulty (float): Highest share difficulty submitted
        last_share_difficulty (float): Difficulty of the last submitted share
    """

    ip: str
    worker_name: Optional[str] = None
    connected_at: float = field(default_factory=time.time)
    accepted: int = 0
    rejected: int = 0
    difficulty: float = 1.0
    pool_difficulty: float = 0.0
    recent_shares: deque = field(default_factory=lambda: deque())
    highest_difficulty: float = 0.0
    last_share_difficulty: float = 0.0
    pool_name: str = "unknown"

    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0

    last_hashrate: float = 0.0
    last_hashrate_time: float = 0.0

    def _categorize_rejection(self, error: Optional[str]) -> None:
        if not error:
            self.rejected_other += 1
            return
            
        try:
            error_data = json.loads(error)
            if isinstance(error_data, list) and len(error_data) > 0:
                error_code = error_data[0]
                error_msg = error_data[1] if len(error_data) > 1 else ""
                
                if error_code == ERROR_JOB_NOT_FOUND:
                    self.rejected_stale += 1
                elif error_code == ERROR_DUPLICATE_SHARE:
                    self.rejected_duplicate += 1
                elif error_code == ERROR_LOW_DIFFICULTY:
                    self.rejected_low_diff += 1
                elif isinstance(error_msg, str) and "above target" in error_msg.lower():
                    self.rejected_low_diff += 1
                else:
                    self.rejected_other += 1
            else:
                self.rejected_other += 1
        except:
            self.rejected_other += 1

    def _cleanup_old_shares(self) -> None:
        """
        Remove shares older than 6 minutes
        """
        if not self.recent_shares:
            return
            
        now = time.time()
        ten_min_ago = now - 600
        
        recent = [(t, d) for t, d in self.recent_shares if t > ten_min_ago]
        
        if len(self.recent_shares) > len(recent) * 1.25:
            self.recent_shares.clear()
            self.recent_shares.extend(recent)
            logger.debug(f"Cleaned up old shares for {self.ip}: {len(self.recent_shares)} -> {len(recent)}")

    def record_share(
        self, accepted: bool, difficulty: float, share_difficulty: float, pool: str, error: Optional[str] = None
    ) -> None:
        """
        Record a submitted share and its result.

        Args:
            accepted (bool): Whether the share was accepted by the pool
            difficulty (float): Difficulty level of the share
            share_difficulty (float): Share difficulty level
            pool (str): Name of the pool
            error (Optional[str]): Error message if the share was rejected
        """
        if accepted:
            self.accepted += 1
            self.recent_shares.append((time.time(), difficulty))
            logger.debug(f"Accepted share from {self.ip} at difficulty {difficulty}")
        else:
            self.rejected += 1
            self._categorize_rejection(error)
            logger.debug(
                f"Rejected share from {self.ip} at difficulty {difficulty} with error {error}"
            )

        if (self.accepted + self.rejected) % 100 == 0:
            self._cleanup_old_shares()

        self.last_share_difficulty = share_difficulty
        if share_difficulty > self.highest_difficulty:
            self.highest_difficulty = share_difficulty
            logger.info(f"New highest difficulty for {self.ip}: {share_difficulty}")


    def update_difficulty(self, difficulty: float) -> None:
        """
        Update the miner's current difficulty level.

        Args:
            difficulty (float): New difficulty level
        """
        logger.debug(f"Updated difficulty for {self.ip} to {difficulty}")
        self.difficulty = difficulty

    def get_hashrate(self) -> float:
        """
        Calculate estimated hashrate based on recent shares.

        Uses a sliding 5-minute window with the formula:
        hashrate = (sum of difficulties * 2^32) / 300 seconds

        Returns:
            float: Estimated hashrate in hashes per second
        """
        if not self.recent_shares:
            return 0.0
        
        now = time.time()
        five_min_ago = now - 300
        
        recent = [(t, d) for t, d in self.recent_shares if t > five_min_ago]
        
        if not recent:
            return 0.0
        
        if len(recent) < 10:
            return 0.0
            
        time_span = 300.0
        total_hashes = sum(diff * (2**32) for _, diff in recent)
        
        return total_hashes / time_span


class StatsManager:
    """
    Central manager for all connected miner statistics.

    Maintains a registry of all active miners and provides methods to
    register/unregister miners and retrieve aggregated statistics.
    """

    def __init__(self):
        """Initialize an empty miners registry."""
        self.miners: dict[str, MinerStats] = {}
        logger.info("StatsManager initialized")

    def register_miner(self, peer: tuple[str, int], pool_name: str = "unknown") -> MinerStats:
        """
        Register a new miner connection and create its statistics tracker.

        Args:
            peer: (ip, port) tuple from socket connection
            pool_name: Name of the pool this miner is connected to

        Returns:
            MinerStats: Newly created statistics object for this miner
        """
        key = f"{peer[0]}:{peer[1]}"
        stats = MinerStats(ip=peer[0], pool_name=pool_name)
        self.miners[key] = stats
        logger.debug(f"Registered miner: {key} on pool {pool_name}")
        return stats

    def unregister_miner(self, peer: tuple[str, int]) -> None:
        """
        Remove a miner from the registry when they disconnect.

        Args:
            peer: (ip, port) tuple from socket connection
        """
        key = f"{peer[0]}:{peer[1]}"
        self.miners.pop(key, None)
        logger.debug(f"Unregistered miner: {key}")

    def get_all_stats(self) -> list[dict]:
        """
        Retrieve statistics for all connected miners.

        Returns:
            list: List of formatted miner statistics
        """
        stats: list[dict] = []
        for key, s in self.miners.items():
            stats.append(
                {
                    "miner": key,
                    "worker": s.worker_name or "",
                    "accepted": s.accepted,
                    "rejected": s.rejected,
                    "difficulty": s.difficulty,
                    "pool_difficulty": s.pool_difficulty,
                    "hashrate": s.get_hashrate(),
                    "highest_difficulty": s.highest_difficulty,
                    "last_share_difficulty": s.last_share_difficulty,
                    "pool": s.pool_name,
                    "rejected_breakdown": {
                        "stale": s.rejected_stale,
                        "duplicate": s.rejected_duplicate,
                        "low_diff": s.rejected_low_diff,
                        "other": s.rejected_other,
                    },
                }
            )
        return stats
