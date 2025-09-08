"""General configuration queries and operations."""

from typing import Any, Optional

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_general_config(db: StatsDB) -> Optional[dict[str, Any]]:
    """Get current general configuration. Returns None if not found."""

    query = """
    SELECT worker_percentage, updated_at
    FROM general_config 
    WHERE id = 1
    """

    result = await db.client.query(query)

    if not result.result_rows:
        return None

    row = result.result_rows[0]
    return {
        "worker_percentage": float(row[0]) if row[0] is not None else None,
        "updated_at": row[1],
    }


async def update_general_config(
    db: StatsDB,
    worker_percentage: Optional[float] = None,
) -> dict[str, Any]:
    """
    Update general configuration with provided fields.

    Args:
        db: Database connection
        worker_percentage: New worker percentage value (optional)

    Returns:
        Dictionary with updated field names and values
    """

    update_fields = []
    params = {}

    if worker_percentage is not None:
        update_fields.append("worker_percentage = %(worker_percentage)s")
        params["worker_percentage"] = worker_percentage

    if not update_fields:
        raise ValueError("At least one field must be provided for update")

    update_fields.append("updated_at = now()")

    update_query = f"""
    ALTER TABLE general_config 
    UPDATE {", ".join(update_fields)}
    WHERE id = 1
    """
    await db.client.command(update_query, parameters=params)

    updated_fields = {}
    if worker_percentage is not None:
        updated_fields["worker_percentage"] = worker_percentage

    return updated_fields
