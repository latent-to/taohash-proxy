"""Balance queries for user_rewards table."""

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_worker_balance(db: StatsDB, worker: str) -> Optional[Dict[str, Any]]:
    """
    Get current balance for a specific worker.

    Args:
        db: Database connection
        worker: Worker name

    Returns:
        Balance information or None if worker not found
    """
    try:
        balance_query = """
        SELECT worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by
        FROM user_rewards
        WHERE worker = %(worker)s
        ORDER BY last_updated DESC
        LIMIT 1
        """

        result = await db.client.query(balance_query, parameters={"worker": worker})

        if not result.result_rows:
            return None

        row = result.result_rows[0]
        return {
            "worker": row[0],
            "unpaid_amount": float(row[1]),
            "paid_amount": float(row[2]),
            "total_earned": float(row[3]),
            "last_updated": row[4],
            "updated_by": row[5],
        }

    except Exception as e:
        logger.error(f"Failed to get balance for worker {worker}: {e}")
        raise


async def get_all_worker_balances(
    db: StatsDB, limit: Optional[int] = None, offset: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Get current balances for all workers.

    Args:
        db: Database connection
        limit: Maximum number of records to return
        offset: Number of records to skip

    Returns:
        List of balance information
    """
    try:
        # Optional pagination
        base_query = """
        SELECT worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by
        FROM (
            SELECT worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by,
                   ROW_NUMBER() OVER (PARTITION BY worker ORDER BY last_updated DESC) as rn
            FROM user_rewards
        ) ranked
        WHERE rn = 1
        ORDER BY worker
        """

        if limit is not None:
            base_query += f" LIMIT {limit}"
            if offset is not None:
                base_query += f" OFFSET {offset}"

        result = await db.client.query(base_query)

        balances = []
        for row in result.result_rows:
            balances.append(
                {
                    "worker": row[0],
                    "unpaid_amount": float(row[1]),
                    "paid_amount": float(row[2]),
                    "total_earned": float(row[3]),
                    "last_updated": row[4],
                    "updated_by": row[5],
                }
            )

        logger.debug(f"Retrieved {len(balances)} worker balances")
        return balances

    except Exception as e:
        logger.error(f"Failed to get all worker balances: {e}")
        raise


async def update_worker_balance_manually(
    db: StatsDB, worker: str, balance_request, admin_user: str
) -> Dict[str, Any]:
    """
    Update worker balance fields - only update fields that are provided.
    Uses INSERT with ReplacingMergeTree pattern.
    """
    try:
        current_balance_query = """
        SELECT worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by
        FROM user_rewards
        WHERE worker = %(worker)s
        ORDER BY last_updated DESC
        LIMIT 1
        """

        result = await db.client.query(
            current_balance_query, parameters={"worker": worker}
        )
        if not result.result_rows:
            raise ValueError(f"Worker {worker} not found")

        current = result.result_rows[0]

        # Use provided values or keep current ones
        new_balance_data = {
            "worker": worker,
            "unpaid_amount": (
                balance_request.unpaid_amount
                if balance_request.unpaid_amount is not None
                else float(current[1])
            ),
            "paid_amount": (
                balance_request.paid_amount
                if balance_request.paid_amount is not None
                else float(current[2])
            ),
            "total_earned": (
                balance_request.total_earned
                if balance_request.total_earned is not None
                else float(current[3])
            ),
            "last_updated": datetime.now(timezone.utc),
            "updated_by": "admin_manual_edit",
        }

        balance_insert = """
        INSERT INTO user_rewards (
            worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by
        ) VALUES (
            %(worker)s, %(unpaid_amount)s, %(paid_amount)s, %(total_earned)s, %(last_updated)s, %(updated_by)s
        )
        """

        await db.client.command(balance_insert, parameters=new_balance_data)

        logger.info(
            f"Balance updated for {worker}: "
            f"unpaid: {float(current[1]):.8f} -> {new_balance_data['unpaid_amount']:.8f}, "
            f"paid: {float(current[2]):.8f} -> {new_balance_data['paid_amount']:.8f}, "
            f"total: {float(current[3]):.8f} -> {new_balance_data['total_earned']:.8f} | "
            f"Updated by: {admin_user[:5]} | Reason: {balance_request.reason}"
        )

        return new_balance_data

    except Exception as e:
        logger.error(f"Failed to update balance for {worker}: {e}")
        raise
