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

