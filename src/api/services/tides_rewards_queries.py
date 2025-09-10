"""TIDES rewards queries and operations."""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional

from src.api.services.tides_queries import calculate_custom_tides_window
from src.utils.time_normalize import normalize_tides_window_snapshot
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


async def update_tides_reward(
    db: StatsDB,
    tx_hash: str,
    btc_amount: Optional[float] = None,
    processed: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Update TIDES reward fields using ALTER TABLE UPDATE.
    
    Args:
        db: Database connection
        tx_hash: Transaction hash to update
        btc_amount: New BTC amount (optional)
        processed: New processed status (optional)
    
    Returns:
        Dictionary with updated field names and values
    """
    
    # Check if reward exists
    check_query = "SELECT tx_hash FROM tides_rewards WHERE tx_hash = %(tx_hash)s LIMIT 1"
    result = await db.client.query(check_query, {"tx_hash": tx_hash})
    
    if not result.result_rows:
        return None  # Reward not found
    
    # Build update fields
    update_fields = []
    params = {"tx_hash": tx_hash}
    
    if btc_amount is not None:
        update_fields.append("btc_amount = %(btc_amount)s")
        params["btc_amount"] = btc_amount
    
    if processed is not None:
        update_fields.append("processed = %(processed)s")
        params["processed"] = processed
    
    if not update_fields:
        raise ValueError("At least one field must be provided for update")
    
    # Execute update (don't update updated_at as it's the version column)
    update_query = f"""
    ALTER TABLE tides_rewards 
    UPDATE {", ".join(update_fields)}
    WHERE tx_hash = %(tx_hash)s
    """
    
    await db.client.command(update_query, parameters=params)
    
    # Return updated fields
    updated_fields = {}
    if btc_amount is not None:
        updated_fields["btc_amount"] = btc_amount
    if processed is not None:
        updated_fields["processed"] = processed
    
    return updated_fields


async def create_tides_reward(
    db: StatsDB,
    tx_hash: str,
    block_height: int,
    btc_amount: float,
    confirmed_at: datetime,
) -> dict[str, Any]:
    """
    Create a new TIDES reward with calculated window at the confirmed datetime.
    
    Args:
        db: Database connection
        tx_hash: Transaction hash
        block_height: Bitcoin block height
        btc_amount: BTC reward amount
        confirmed_at: When the transaction was confirmed
    
    Returns:
        Dictionary with the created reward data
    """
    
    check_query = "SELECT tx_hash FROM tides_rewards WHERE tx_hash = %(tx_hash)s LIMIT 1"
    result = await db.client.query(check_query, {"tx_hash": tx_hash})
    
    if result.result_rows:
        raise ValueError(f"TIDES reward with tx_hash {tx_hash} already exists")
    
    # Ensure confirmed_at is UTC-aware
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
    else:
        confirmed_at = confirmed_at.astimezone(timezone.utc)


    tides_window = await calculate_custom_tides_window(db, confirmed_at)
    tides_window = normalize_tides_window_snapshot(tides_window, default_updated_at=confirmed_at)
    tides_window_json = json.dumps(tides_window)
    
    insert_query = """
    INSERT INTO tides_rewards (
        tx_hash, block_height, btc_amount, 
        confirmed_at, discovered_at, tides_window
    )
    VALUES (
        %(tx_hash)s, %(block_height)s, %(btc_amount)s, 
        %(confirmed_at)s, %(discovered_at)s, %(tides_window)s
    )
    """
    
    params = {
        "tx_hash": tx_hash,
        "block_height": block_height,
        "btc_amount": btc_amount,
        "confirmed_at": confirmed_at,
        "discovered_at": confirmed_at,
        "tides_window": tides_window_json,
    }
    
    await db.client.command(insert_query, parameters=params)
    
    logger.info(f"Created TIDES reward: {tx_hash} (Block {block_height}, {btc_amount} BTC)")
    
    return {
        "tx_hash": tx_hash,
        "block_height": block_height,
        "btc_amount": btc_amount,
        "confirmed_at": confirmed_at,
        "discovered_at": confirmed_at,
        "tides_window": tides_window,
        "processed": False,
    }
