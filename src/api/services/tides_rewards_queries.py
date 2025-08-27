"""TIDES rewards queries and operations."""

import json
from typing import Any, List, Optional

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_all_tides_rewards(db: StatsDB) -> List[dict[str, Any]]:
    """Get summary of all TIDES rewards ordered by newest first."""
    
    query = """
    SELECT 
        tx_hash,
        btc_amount,
        confirmed_at,
        processed
    FROM tides_rewards
    ORDER BY confirmed_at DESC
    """
    
    result = await db.client.query(query)
    
    rewards = []
    for row in result.result_rows:
        rewards.append({
            "tx_hash": row[0],
            "btc_amount": float(row[1]),
            "confirmed_at": row[2],
            "processed": bool(row[3]),
        })
    
    return rewards


async def get_tides_reward_by_tx_hash(db: StatsDB, tx_hash: str) -> Optional[dict[str, Any]]:
    """Get full TIDES reward details by transaction hash."""
    
    query = """
    SELECT 
        tx_hash,
        block_height,
        btc_amount,
        confirmed_at,
        discovered_at,
        tides_window,
        processed,
        updated_at
    FROM tides_rewards
    WHERE tx_hash = %(tx_hash)s
    LIMIT 1
    """
    
    result = await db.client.query(query, parameters={"tx_hash": tx_hash})
    
    if not result.result_rows:
        return None
    
    row = result.result_rows[0]
    
    # Parse TIDES window JSON
    tides_window_data = {}
    if row[5]:  # tides_window
        try:
            tides_window_data = json.loads(row[5])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse TIDES window for {tx_hash}: {e}")
            tides_window_data = {}
    
    return {
        "tx_hash": row[0],
        "block_height": int(row[1]),
        "btc_amount": float(row[2]),
        "confirmed_at": row[3],
        "discovered_at": row[4],
        "tides_window": tides_window_data,
        "processed": bool(row[6]),
        "updated_at": row[7],
    }
