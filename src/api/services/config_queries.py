"""Configuration queries and operations."""

from typing import Any, Optional

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_config(db: StatsDB) -> Optional[dict[str, Any]]:
    """Get current TIDES configuration. Returns None if not found."""

    query = """
    SELECT multiplier, network_difficulty, updated_at
    FROM tides_config 
    WHERE id = 1
    """

    result = await db.client.query(query)

    if not result.result_rows:
        return None

    row = result.result_rows[0]
    return {
        "multiplier": float(row[0]) if row[0] is not None else None,
        "network_difficulty": float(row[1]) if row[1] is not None else None,
        "updated_at": row[2],
    }


async def update_config(
    db: StatsDB,
    network_difficulty: Optional[float] = None,
    multiplier: Optional[float] = None,
) -> dict[str, Any]:
    """
    Update TIDES configuration with provided fields.

    Args:
        db: Database connection
        network_difficulty: New network difficulty value (optional)
        multiplier: New multiplier value (optional)

    Returns:
        Dictionary with updated field names and values
    """

    update_fields = []
    params = {}

    if network_difficulty is not None:
        update_fields.append("network_difficulty = %(network_difficulty)s")
        params["network_difficulty"] = network_difficulty

    if multiplier is not None:
        update_fields.append("multiplier = %(multiplier)s")
        params["multiplier"] = multiplier

    if not update_fields:
        raise ValueError("At least one field must be provided for update")

    update_fields.append("updated_at = now()")

    update_query = f"""
    ALTER TABLE tides_config 
    UPDATE {", ".join(update_fields)}
    WHERE id = 1
    """
    await db.client.command(update_query, parameters=params)

    updated_fields = {}
    if network_difficulty is not None:
        updated_fields["network_difficulty"] = network_difficulty
    if multiplier is not None:
        updated_fields["multiplier"] = multiplier

    return updated_fields
