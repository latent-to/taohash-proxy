"""Earnings queries and operations."""

import json
import uuid
from datetime import datetime
from typing import Any, List, Optional

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_worker_earnings(
    db: StatsDB,
    worker: str,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    earning_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[dict[str, Any]]:
    """
    Get earnings for a specific worker with optional filters.
    
    Args:
        db: Database connection
        worker: Worker name
        limit: Maximum number of results
        offset: Number of results to skip  
        earning_type: Filter by earning type ('tides', 'pplns', 'manual')
        date_from: Filter earnings from this date
        date_to: Filter earnings to this date
    
    Returns:
        List of earning records
    """
    try:
        # Build query with filters
        conditions = ["worker = %(worker)s"]
        params = {"worker": worker}

        if earning_type:
            conditions.append("earning_type = %(earning_type)s")
            params["earning_type"] = earning_type

        if date_from:
            conditions.append("earned_at >= %(date_from)s")
            params["date_from"] = date_from

        if date_to:
            conditions.append("earned_at <= %(date_to)s")
            params["date_to"] = date_to

        where_clause = " AND ".join(conditions)
        
        query = f"""
        SELECT 
            earning_id, worker, btc_amount, earning_type, reference,
            tides_reward_id, metadata, earned_at, created_at
        FROM user_earnings
        WHERE {where_clause}
        ORDER BY earned_at DESC
        """

        if limit:
            query += f" LIMIT {limit}"
        if offset:
            query += f" OFFSET {offset}"

        result = await db.client.query(query, parameters=params)

        earnings = []
        for row in result.result_rows:
            # Parse metadata JSON
            metadata_dict = {}
            if row[6]:  # metadata column
                try:
                    metadata_dict = json.loads(row[6])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"Failed to parse metadata for earning {row[0]}")
                    metadata_dict = {}

            earnings.append({
                "earning_id": row[0],
                "worker": row[1],
                "btc_amount": float(row[2]),
                "earning_type": row[3],
                "reference": row[4],
                "tides_reward_id": row[5],
                "metadata": metadata_dict,
                "earned_at": row[7],
                "created_at": row[8],
            })

        logger.debug(f"Retrieved {len(earnings)} earnings for worker {worker}")
        return earnings

    except Exception as e:
        logger.error(f"Failed to get earnings for worker {worker}: {e}")
        raise


async def create_manual_earning(
    db: StatsDB,
    worker: str,
    btc_amount: float,
    earning_type: str = "manual",
    reference: Optional[str] = None,
    metadata: Optional[dict] = None,
    earned_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Create a manual earning record.
    
    Args:
        db: Database connection
        worker: Worker name
        btc_amount: BTC amount earned
        earning_type: Type of earning (default 'manual')
        reference: Optional reference
        metadata: Optional metadata dict
        earned_at: When earned (defaults to now)
    
    Returns:
        Created earning record
    """
    try:
        earning_id = str(uuid.uuid4())
        earned_timestamp = earned_at or datetime.now()
        metadata_json = json.dumps(metadata or {})

        # Insert earning record
        insert_query = """
        INSERT INTO user_earnings (
            earning_id, worker, btc_amount, earning_type, reference,
            tides_reward_id, metadata, earned_at, created_at
        ) VALUES (
            %(earning_id)s, %(worker)s, %(btc_amount)s, %(earning_type)s,
            %(reference)s, %(tides_reward_id)s, %(metadata)s, %(earned_at)s, %(created_at)s
        )
        """

        params = {
            "earning_id": earning_id,
            "worker": worker,
            "btc_amount": btc_amount,
            "earning_type": earning_type,
            "reference": reference,
            "tides_reward_id": None,  # Manual earnings not linked to TIDES
            "metadata": metadata_json,
            "earned_at": earned_timestamp,
            "created_at": datetime.now(),
        }

        await db.client.command(insert_query, parameters=params)

        # Update user balance
        await update_user_balance(db, worker, btc_amount, "manual_earning")

        logger.info(f"Created manual earning {earning_id} for {worker}: {btc_amount} BTC")

        return {
            "earning_id": earning_id,
            "worker": worker,
            "btc_amount": btc_amount,
            "earning_type": earning_type,
            "reference": reference,
            "tides_reward_id": None,
            "metadata": metadata or {},
            "earned_at": earned_timestamp,
            "created_at": params["created_at"],
        }

    except Exception as e:
        logger.error(f"Failed to create manual earning for {worker}: {e}")
        raise


async def update_earning(
    db: StatsDB,
    earning_id: str,
    btc_amount: Optional[float] = None,
    metadata: Optional[dict] = None,
    reference: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Update an existing earning record.
    
    Args:
        db: Database connection
        earning_id: Earning ID to update
        btc_amount: New BTC amount
        metadata: New metadata dict
        reference: New reference
    
    Returns:
        Updated earning record or None if not found
    """
    try:
        # First, get current earning to calculate balance difference
        current_query = """
        SELECT worker, btc_amount, earning_type, reference, metadata, earned_at
        FROM user_earnings
        WHERE earning_id = %(earning_id)s
        LIMIT 1
        """

        result = await db.client.query(current_query, parameters={"earning_id": earning_id})

        if not result.result_rows:
            logger.warning(f"Earning not found: {earning_id}")
            return None

        current_row = result.result_rows[0]
        worker = current_row[0]
        current_amount = float(current_row[1])
        current_earning_type = current_row[2]
        current_reference = current_row[3]
        current_metadata = current_row[4]
        earned_at = current_row[5]

        # Parse current metadata
        try:
            current_metadata_dict = json.loads(current_metadata) if current_metadata else {}
        except (json.JSONDecodeError, TypeError):
            current_metadata_dict = {}

        # Prepare update fields
        new_amount = btc_amount if btc_amount is not None else current_amount
        new_metadata = metadata if metadata is not None else current_metadata_dict
        new_reference = reference if reference is not None else current_reference

        # Calculate balance difference
        balance_diff = new_amount - current_amount

        # Delete old record and insert new one (ClickHouse pattern)
        delete_query = """
        ALTER TABLE user_earnings
        DELETE WHERE earning_id = %(earning_id)s
        """
        await db.client.command(delete_query, parameters={"earning_id": earning_id})

        # Insert updated record
        insert_query = """
        INSERT INTO user_earnings (
            earning_id, worker, btc_amount, earning_type, reference,
            tides_reward_id, metadata, earned_at, created_at
        ) VALUES (
            %(earning_id)s, %(worker)s, %(btc_amount)s, %(earning_type)s,
            %(reference)s, %(tides_reward_id)s, %(metadata)s, %(earned_at)s, %(created_at)s
        )
        """

        params = {
            "earning_id": earning_id,
            "worker": worker,
            "btc_amount": new_amount,
            "earning_type": current_earning_type,
            "reference": new_reference,
            "tides_reward_id": None,
            "metadata": json.dumps(new_metadata),
            "earned_at": earned_at,
            "created_at": datetime.now(),
        }

        await db.client.command(insert_query, parameters=params)

        # Update user balance if amount changed
        if balance_diff != 0:
            await update_user_balance(db, worker, balance_diff, "earning_update")

        logger.info(f"Updated earning {earning_id} for {worker} (amount: {new_amount} BTC)")

        return {
            "earning_id": earning_id,
            "worker": worker,
            "btc_amount": new_amount,
            "earning_type": current_earning_type,
            "reference": new_reference,
            "tides_reward_id": None,
            "metadata": new_metadata,
            "earned_at": earned_at,
            "created_at": params["created_at"],
        }

    except Exception as e:
        logger.error(f"Failed to update earning {earning_id}: {e}")
        raise

