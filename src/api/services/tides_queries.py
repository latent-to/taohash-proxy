"""TIDES window queries and operations."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from src.api.services.config_queries import get_config
from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_tides_window(db: StatsDB) -> Optional[dict[str, Any]]:
    """Get TIDES window results. Returns None if not found."""

    query = """
    SELECT share_log_window, network_difficulty, multiplier, window_start,
           window_end, total_difficulty_in_window, total_workers,
           workers_json, updated_at
    FROM tides_window 
    WHERE id = 1
    LIMIT 1
    """

    result = await db.client.query(query)

    if not result.result_rows:
        return None

    row = result.result_rows[0]

    try:
        workers_data = json.loads(row[7]) if row[7] else []
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse workers_json from TIDES cache")
        workers_data = []

    return {
        "workers": workers_data,
        "share_log_window": float(row[0]),
        "network_difficulty": float(row[1]),
        "multiplier": float(row[2]),
        "window_start": row[3] if row[3] else None,
        "window_end": row[4] if row[4] else None,
        "total_difficulty_in_window": float(row[5]),
        "total_workers": int(row[6]),
        "updated_at": row[8] if row[8] else None,
    }


async def _get_window_end_timestamp(db: StatsDB) -> datetime:
    """Get the most recent share timestamp from today (when MV was last updated)"""
    query = """
    SELECT max(ts) as latest_timestamp
    FROM shares
    WHERE toDate(ts) = today()
    """

    result = await db.client.query(query)
    if result.result_rows and result.result_rows[0][0]:
        return result.result_rows[0][0]

    # Fallback
    return datetime.now(timezone.utc)


async def calculate_and_store_tides_window(db: StatsDB) -> dict[str, Any]:
    """
    Calculate TIDES window using daily blocks approach and store results.

    Returns:
        Dictionary with TIDES window data
    """

    config = await get_config(db)
    if not config:
        raise Exception("No TIDES configuration found")

    target_difficulty = config["network_difficulty"] * config["multiplier"]

    # Find included days using daily aggregates
    included_days_info = await _find_included_days(db, target_difficulty)

    # Get included days data (from MV)
    included_days_data = await _fetch_full_days_data(
        db, included_days_info["included_days"]
    )

    # Get start date data (from raw shares)
    start_date_data, start_date_timestamp = await _fetch_start_date_data(
        db, included_days_info["start_date"], included_days_info["remaining_target"]
    )

    complete_data = _merge_worker_totals(included_days_data, start_date_data)

    # Format workers response
    workers_list = _format_workers_response(complete_data)
    total_difficulty = sum(w["share_value"] for w in complete_data.values())
    window_end_timestamp = await _get_window_end_timestamp(db)
    window_start_timestamp = start_date_timestamp

    # Fallback for start date if not found
    if not window_start_timestamp and included_days_info["included_days"]:
        earliest_date = min(included_days_info["included_days"])
        query = "SELECT min(ts) FROM shares WHERE toDate(ts) = %(date)s"
        result = await db.client.query(query, {"date": earliest_date})
        if result.result_rows and result.result_rows[0][0]:
            window_start_timestamp = result.result_rows[0][0]

    tides_data = {
        "workers": workers_list,
        "share_log_window": target_difficulty,
        "network_difficulty": config["network_difficulty"],
        "multiplier": config["multiplier"],
        "window_start": window_start_timestamp.isoformat()
        if window_start_timestamp
        else None,
        "window_end": window_end_timestamp.isoformat(),
        "total_difficulty_in_window": total_difficulty,
        "total_workers": len(workers_list),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    await _store_tides_window(db, tides_data)

    return tides_data


async def _find_included_days(
    db: StatsDB, target_difficulty: float, end_date: Optional[datetime.date] = None
) -> dict[str, Any]:
    """
    Find included days using daily totals.

    Args:
        target_difficulty: Target difficulty to accumulate
        end_date: End date to work backwards from. If None, uses today()
    """

    tides_start_date = os.environ.get("TIDES_START_DATE", "2025-09-27")
    
    if end_date is None:
        # Work backwards from latest shares - default
        query = """
        SELECT date, COALESCE(sumMerge(pool_difficulty_sum), 0) as daily_total
        FROM worker_daily_share_value
        WHERE date >= %(tides_start_date)s
        GROUP BY date 
        ORDER BY date DESC
        """
        params = {"tides_start_date": tides_start_date}
    else:
        # Work backwards from specified end_date (excluding it), for custom calculations
        query = """
        SELECT date, COALESCE(sumMerge(pool_difficulty_sum), 0) as daily_total
        FROM worker_daily_share_value
        WHERE date >= %(start_limit)s AND date < %(end_date)s
        GROUP BY date 
        ORDER BY date DESC
        """
        params = {"start_limit": tides_start_date, "end_date": end_date}

    result = await db.client.query(query, parameters=params)

    cumulative_difficulty = 0
    included_days = []
    start_date = None

    for row in result.result_rows:
        date, daily_total = row[0], float(row[1])

        if cumulative_difficulty + daily_total <= target_difficulty:
            included_days.append(date)
            cumulative_difficulty += daily_total
        else:
            start_date = date
            break

    return {
        "included_days": included_days,
        "start_date": start_date,
        "remaining_target": target_difficulty - cumulative_difficulty,
    }


async def _fetch_full_days_data(db: StatsDB, included_days: list) -> dict[str, dict]:
    """Get aggregated worker data for complete days"""

    if not included_days:
        return {}

    query = """
    SELECT worker, COALESCE(countMerge(shares), 0) as total_shares, COALESCE(sumMerge(pool_difficulty_sum), 0) as total_share_value
    FROM worker_daily_share_value
    WHERE date IN %(dates)s
    GROUP BY worker
    """

    result = await db.client.query(query, {"dates": included_days})

    return {
        row[0]: {"shares": int(row[1]), "share_value": float(row[2])}
        for row in result.result_rows
    }


async def _fetch_start_date_data(
    db: StatsDB, start_date, remaining_target: float
) -> tuple[dict[str, dict], datetime]:
    """Get data from start date, working backwards until target reached"""

    if not start_date or remaining_target <= 0:
        return {}, None

    query = """
    SELECT worker, pool_difficulty, ts
    FROM shares 
    WHERE toDate(ts) = %(start_date)s
    ORDER BY ts DESC
    """

    result = await db.client.query(query, {"start_date": start_date})

    workers = {}
    cumulative_difficulty = 0
    start_date_timestamp = None

    for row in result.result_rows:
        worker, difficulty, ts = row[0], float(row[1]), row[2]

        if cumulative_difficulty + difficulty >= remaining_target:
            break

        if worker not in workers:
            workers[worker] = {"shares": 0, "share_value": 0.0}

        workers[worker]["shares"] += 1
        workers[worker]["share_value"] += difficulty
        cumulative_difficulty += difficulty

        # Track the starting timestamp
        start_date_timestamp = ts

    return workers, start_date_timestamp


def _merge_worker_totals(complete_workers: dict, start_date_data: dict) -> dict:
    """Merge complete days + start date data"""

    final_workers = complete_workers.copy()

    for worker, data in start_date_data.items():
        if worker in final_workers:
            final_workers[worker]["shares"] += data["shares"]
            final_workers[worker]["share_value"] += data["share_value"]
        else:
            final_workers[worker] = data

    return final_workers


def _format_workers_response(complete_data: dict) -> list:
    """Format worker data into the standard response format with percentages"""
    
    total_difficulty = sum(w["share_value"] for w in complete_data.values())
    workers_list = []

    for worker_name, worker_data in complete_data.items():
        percentage = (
            (worker_data["share_value"] / total_difficulty) * 100
            if total_difficulty > 0
            else 0
        )
        workers_list.append(
            {
                "name": worker_name,
                "shares": worker_data["shares"],
                "share_value": worker_data["share_value"],
                "percentage": percentage,
            }
        )

    workers_list.sort(key=lambda x: x["share_value"], reverse=True)
    return workers_list


async def _store_tides_window(db: StatsDB, tides_data: dict[str, Any]) -> None:
    """Store TIDES results in database using ALTER TABLE UPDATE"""

    workers_json = json.dumps(tides_data["workers"])

    check_query = """
    SELECT id FROM tides_window WHERE id = 1 LIMIT 1
    """

    result = await db.client.query(check_query)

    if result.result_rows:
        # Update existing
        update_query = """
        ALTER TABLE tides_window
        UPDATE 
            share_log_window = %(share_log_window)s,
            network_difficulty = %(network_difficulty)s,
            multiplier = %(multiplier)s,
            window_start = %(window_start)s,
            window_end = %(window_end)s,
            total_difficulty_in_window = %(total_difficulty)s,
            total_workers = %(total_workers)s,
            workers_json = %(workers_json)s,
            updated_at = now()
        WHERE id = 1
        """
    else:
        # Insert
        update_query = """
        INSERT INTO tides_window
        (id, share_log_window, network_difficulty, multiplier, window_start, window_end,
         total_difficulty_in_window, total_workers, workers_json, updated_at)
        VALUES (1, %(share_log_window)s, %(network_difficulty)s, %(multiplier)s, 
                %(window_start)s, %(window_end)s, %(total_difficulty)s, 
                %(total_workers)s, %(workers_json)s, now())
        """

    params = {
        "share_log_window": tides_data["share_log_window"],
        "network_difficulty": tides_data["network_difficulty"],
        "multiplier": tides_data["multiplier"],
        "window_start": datetime.fromisoformat(tides_data["window_start"])
        if tides_data["window_start"]
        else datetime.now(timezone.utc),
        "window_end": datetime.fromisoformat(tides_data["window_end"]),
        "total_difficulty": tides_data["total_difficulty_in_window"],
        "total_workers": tides_data["total_workers"],
        "workers_json": workers_json,
    }

    await db.client.command(update_query, parameters=params)


async def calculate_custom_tides_window(
    db: StatsDB, end_datetime: datetime
) -> dict[str, Any]:
    """
    Calculate TIDES window from a specific datetime.

    Follows the flow:
    SPECIFIED_TIME → Remaining Day (partial) → Full Days → Start Date (partial) → Target Reached

    Args:
        db: Database connection
        end_datetime: Calculate window backwards from this time

    Returns:
        Dictionary with TIDES window data
    """

    config = await get_config(db)
    if not config:
        raise Exception("No TIDES configuration found")

    target_difficulty = config["network_difficulty"] * config["multiplier"]

    # Remaining End Day (partial) - consume from end_datetime backwards on same day
    (
        remaining_day_data,
        consumed_difficulty,
        earliest_end_day_timestamp,
    ) = await _fetch_end_day_data(db, end_datetime, target_difficulty)

    if consumed_difficulty >= target_difficulty:
        complete_data = remaining_day_data
        window_start_timestamp = earliest_end_day_timestamp
    else:
        # Full Days - consume complete previous days
        remaining_target = target_difficulty - consumed_difficulty
        included_days_info = await _find_included_days(
            db, remaining_target, end_datetime.date()
        )

        full_days_data = await _fetch_full_days_data(
            db, included_days_info["included_days"]
        )

        # Start Date (partial) - for remaining target
        start_date_data, start_date_timestamp = await _fetch_start_date_data(
            db, included_days_info["start_date"], included_days_info["remaining_target"]
        )
        complete_data = _merge_worker_totals(
            _merge_worker_totals(remaining_day_data, full_days_data), start_date_data
        )

        window_start_timestamp = start_date_timestamp

        if not window_start_timestamp and included_days_info["included_days"]:
            earliest_date = min(included_days_info["included_days"])
            query = "SELECT min(ts) FROM shares WHERE toDate(ts) = %(date)s"
            result = await db.client.query(query, {"date": earliest_date})
            if result.result_rows and result.result_rows[0][0]:
                window_start_timestamp = result.result_rows[0][0]

        if not window_start_timestamp and earliest_end_day_timestamp:
            window_start_timestamp = earliest_end_day_timestamp

    # Format workers response
    workers_list = _format_workers_response(complete_data)
    total_difficulty = sum(w["share_value"] for w in complete_data.values())

    return {
        "workers": workers_list,
        "share_log_window": target_difficulty,
        "network_difficulty": config["network_difficulty"],
        "multiplier": config["multiplier"],
        "window_start": window_start_timestamp.isoformat()
        if window_start_timestamp
        else None,
        "window_end": end_datetime.isoformat(),
        "total_difficulty_in_window": total_difficulty,
        "total_workers": len(workers_list),
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_end_day_data(
    db: StatsDB, end_datetime: datetime, target_difficulty: float
) -> tuple[dict[str, dict], float, datetime]:
    """
    Get shares from end_datetime backwards to start of that same day.

    Returns:
        - worker_data: Dict of workers and their shares
        - consumed_difficulty: How much difficulty was consumed from that day
        - earliest_timestamp: Earliest share timestamp used from that day
    """

    query = """
    SELECT worker, pool_difficulty, ts
    FROM shares 
    WHERE toDate(ts) = toDate(%(end_datetime)s)
      AND ts <= %(end_datetime)s
    ORDER BY ts DESC
    """

    result = await db.client.query(query, {"end_datetime": end_datetime})

    workers = {}
    consumed_difficulty = 0
    earliest_timestamp = None

    for row in result.result_rows:
        worker, difficulty, ts = row[0], float(row[1]), row[2]

        if consumed_difficulty + difficulty >= target_difficulty:
            break

        if worker not in workers:
            workers[worker] = {"shares": 0, "share_value": 0.0}

        workers[worker]["shares"] += 1
        workers[worker]["share_value"] += difficulty
        consumed_difficulty += difficulty

        earliest_timestamp = ts

    return workers, consumed_difficulty, earliest_timestamp
