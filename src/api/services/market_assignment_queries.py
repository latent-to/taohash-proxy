"""Marketplace miner assignment queries and atomic operations."""

from datetime import datetime, timezone
from typing import Optional

import asyncpg

from src.utils.logger import get_logger

logger = get_logger(__name__)


class ConflictError(Exception):
    """Raised when miner assignment conflicts with existing assignments."""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.conflicts = conflicts


async def create_assignment(
    pool: asyncpg.Pool,
    worker_ip: str,
    order_id: str,
) -> Optional[str]:
    """
    Create a new miner assignment to an order.

    Fails if the worker already has an active assignment - caller must
    expire the existing assignment first via delete_assignment().

    Args:
        pool: asyncpg connection pool
        worker_ip: Miner identifier (worker:ip format)
        order_id: Order identifier

    Returns:
        The new assignment_id (UUID) if created successfully, None if failed
        (e.g., worker already has an active assignment)
    """
    try:
        async with pool.acquire() as conn:
            assignment_id = await conn.fetchval(
                """
                INSERT INTO miner_assignments (worker_ip, order_id)
                VALUES ($1, $2)
                RETURNING assignment_id
                """,
                worker_ip,
                order_id,
            )
        logger.info(
            f"Assigned miner {worker_ip} to order {order_id} (assignment_id={assignment_id})"
        )
        return str(assignment_id)
    except asyncpg.UniqueViolationError:
        logger.warning(f"Worker {worker_ip} already has an active assignment")
        return None
    except Exception as e:
        logger.error(f"Failed to create assignment for {worker_ip}: {e}")
        return None


async def get_miner_assignment(
    pool: asyncpg.Pool,
    worker_ip: str,
) -> Optional[dict]:
    """
    Get the current active assignment for a miner.

    Args:
        pool: asyncpg connection pool
        worker_ip: Miner identifier (worker:ip format)

    Returns:
        Assignment dict or None if not assigned
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT assignment_id, worker_ip, order_id, status, assigned_at
                FROM miner_assignments WHERE worker_ip = $1 AND status = 'active'
                """,
                worker_ip,
            )

            if row:
                return {
                    "assignment_id": str(row["assignment_id"]),
                    "worker_ip": row["worker_ip"],
                    "order_id": row["order_id"],
                    "status": row["status"],
                    "assigned_at": row["assigned_at"],
                }
            return None
    except Exception as e:
        logger.error(f"Failed to get assignment for {worker_ip}: {e}")
        return None


async def get_order_assignments(
    pool: asyncpg.Pool,
    order_id: str,
) -> list[dict]:
    """
    Get all active miner assignments for a specific order.

    Args:
        pool: asyncpg connection pool
        order_id: Order identifier

    Returns:
        List of assignment dicts
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT assignment_id, worker_ip, order_id, status, assigned_at, unassigned_at
                FROM miner_assignments WHERE order_id = $1 AND status = 'active'
                """,
                order_id,
            )

            assignments = []
            for row in rows:
                assignments.append(
                    {
                        "assignment_id": str(row["assignment_id"]),
                        "worker_ip": row["worker_ip"],
                        "order_id": row["order_id"],
                        "status": row["status"],
                        "assigned_at": row["assigned_at"],
                        "unassigned_at": row["unassigned_at"],
                    }
                )
            return assignments
    except Exception as e:
        logger.error(f"Failed to get assignments for order {order_id}: {e}")
        return []


async def delete_assignment(pool: asyncpg.Pool, worker_ip: str) -> bool:
    """
    Mark a miner's active assignment as expired.

    Only affects active assignments to avoid clobbering history.

    Args:
        pool: asyncpg connection pool
        worker_ip: Miner identifier (worker:ip format)

    Returns:
        True if an assignment was expired, False otherwise
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE miner_assignments
                SET status = 'expired', unassigned_at = NOW(), updated_at = NOW()
                WHERE worker_ip = $1 AND status = 'active'
                """,
                worker_ip,
            )
            updated = result != "UPDATE 0"
            if updated:
                logger.info(f"Expired assignment for miner {worker_ip}")
            return updated
    except Exception as e:
        logger.error(f"Failed to delete assignment for {worker_ip}: {e}")
        return False


async def clear_order_assignments(pool: asyncpg.Pool, order_id: str) -> int:
    """
    Clear all active miner assignments for a specific order.

    Args:
        pool: asyncpg connection pool
        order_id: Order identifier

    Returns:
        Number of assignments cleared
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE miner_assignments
                SET status = 'expired', unassigned_at = NOW(), updated_at = NOW()
                WHERE order_id = $1 AND status = 'active'
                """,
                order_id,
            )
            # Result format is "UPDATE N"
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info(f"Cleared {count} assignments for order {order_id}")
            return count
    except Exception as e:
        logger.error(f"Failed to clear assignments for order {order_id}: {e}")
        return 0


async def get_worker_assignment_history(
    pool: asyncpg.Pool,
    worker_ip: str,
) -> list[dict]:
    """
    Get all assignments (active and expired) for a worker.

    Args:
        pool: asyncpg connection pool
        worker_ip: Miner identifier (worker:ip format)

    Returns:
        List of assignment dicts, ordered by assigned_at DESC (newest first)
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT assignment_id, worker_ip, order_id, status,
                       assigned_at, unassigned_at, updated_at
                FROM miner_assignments
                WHERE worker_ip = $1
                ORDER BY assigned_at DESC
                """,
                worker_ip,
            )

            assignments = []
            for row in rows:
                assignments.append(
                    {
                        "assignment_id": str(row["assignment_id"]),
                        "worker_ip": row["worker_ip"],
                        "order_id": row["order_id"],
                        "status": row["status"],
                        "assigned_at": row["assigned_at"],
                        "unassigned_at": row["unassigned_at"],
                        "updated_at": row["updated_at"],
                    }
                )
            return assignments
    except Exception as e:
        logger.error(f"Failed to get assignment history for {worker_ip}: {e}")
        return []


async def assign_miners_atomic(
    pool: asyncpg.Pool,
    order_id: str,
    worker_ips: list[str],
) -> dict:
    """
    Atomically assign miners to an existing order.

    Miners already on THIS order are skipped (idempotent).
    Miners on OTHER orders cause ConflictError.

    Args:
        pool: asyncpg connection pool
        order_id: Order identifier
        worker_ips: List of miner identifiers to assign

    Returns:
        {"assigned": [...], "skipped": [...]} on success

    Raises:
        ValueError: If order doesn't exist, is not active, or has expired
        ConflictError: If any miners are assigned to different orders
    """
    worker_ips = list(dict.fromkeys(worker_ips))

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Verify order exists and is active
            order = await conn.fetchrow(
                """
                SELECT order_id, status, expires_at
                FROM marketplace_orders WHERE order_id = $1
                FOR UPDATE
                """,
                order_id,
            )

            if not order:
                raise ValueError(f"Order {order_id} not found")
            if order["status"] != "active":
                raise ValueError(
                    f"Order {order_id} is not active (status: {order['status']})"
                )
            if order["expires_at"] and order["expires_at"] <= datetime.now(
                timezone.utc
            ):
                raise ValueError(f"Order {order_id} has expired")

            if not worker_ips:
                return {"assigned": [], "skipped": []}

            # Lock and check existing assignments
            existing = await conn.fetch(
                """
                SELECT worker_ip, order_id
                FROM miner_assignments
                WHERE worker_ip = ANY($1) AND status = 'active'
                FOR UPDATE
                """,
                worker_ips,
            )

            conflicts, skipped, assigned_set = [], [], set()
            for row in existing:
                if row["order_id"] == order_id:
                    skipped.append(row["worker_ip"])
                else:
                    conflicts.append(
                        {
                            "worker_ip": row["worker_ip"],
                            "current_order": row["order_id"],
                        }
                    )
                assigned_set.add(row["worker_ip"])

            if conflicts:
                raise ConflictError(
                    "Some miners are already assigned to other orders",
                    conflicts=conflicts,
                )

            to_assign = [w for w in worker_ips if w not in assigned_set]

            if to_assign:
                await conn.executemany(
                    "INSERT INTO miner_assignments (worker_ip, order_id) VALUES ($1, $2)",
                    [(w, order_id) for w in to_assign],
                )

            logger.info(
                f"Assigned {len(to_assign)} miners to {order_id}, skipped {len(skipped)}"
            )
            return {"assigned": to_assign, "skipped": skipped}
