"""Worker statistics queries and operations."""

from datetime import datetime
from typing import Any, Optional

from src.storage.db import StatsDB
from src.utils.logger import get_logger
from src.api.services.general_config_queries import get_general_config

logger = get_logger(__name__)


async def get_worker_counts(db: StatsDB) -> dict[str, int]:
    """Get counts of workers in different states."""
    try:
        query = """
        SELECT 
            COUNT(DISTINCT CASE WHEN last_share_ts > now() - INTERVAL 120 MINUTE THEN worker END) as ok_workers,
            COUNT(DISTINCT CASE WHEN last_share_ts <= now() - INTERVAL 120 MINUTE THEN worker END) as off_workers
        FROM worker_pool_latest_share_mv
        """

        result = await db.client.query(query)

        if result.result_rows and result.result_rows[0]:
            row = result.result_rows[0]
            return {"ok_workers": int(row[0] or 0), "off_workers": int(row[1] or 0)}
    except Exception as e:
        logger.error(f"Error in get_worker_counts: {e}")

    return {"ok_workers": 0, "off_workers": 0}


async def get_worker_stats(
    db: StatsDB,
    worker: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetches all worker statistics which were active in the last 24 hours.
    """
    params = {}
    where_clause = ""

    if worker:
        where_clause = "WHERE worker = %(worker)s"
        params["worker"] = worker

    query = f"""
    WITH
        all_active_workers AS (
            SELECT DISTINCT worker
            FROM worker_stats_24h
            {where_clause}
        ),
        
        stats_5m AS (
            SELECT
                worker,
                argMax(miner, ts) as latest_miner,
                count() as shares,
                sum(pool_difficulty) as share_value,
                sum(pool_difficulty) * 4294967296 / 300 as hashrate
            FROM shares
            WHERE ts > now() - INTERVAL 5 MINUTE
            {"AND " + where_clause.replace("WHERE ", "") if where_clause else ""}
            GROUP BY worker
        )

    SELECT
        w.worker,
        -- Get miner from the most recent source available
        COALESCE(s5.latest_miner, s60.latest_miner, s24.latest_miner) as latest_miner,
        
        -- Get the live state and last share from the fast MV
        toUnixTimestamp(latest_share_data.last_share_ts) as last_share_ts,
        CASE
            WHEN latest_share_data.last_share_ts > now() - INTERVAL 10 MINUTE THEN 'ok'
            ELSE 'offline'
        END as state,

        s5.shares as shares_5m,
        s5.hashrate as hashrate_5m,
        s5.share_value as share_value_5m,

        s60.shares as shares_60m,
        s60.hashrate as hashrate_60m,
        s60.pool_difficulty_sum as share_value_60m,

        s24.shares as shares_24h,
        s24.hashrate as hashrate_24h,
        s24.pool_difficulty_sum as share_value_24h
        
    FROM all_active_workers AS w
    LEFT JOIN worker_stats_24h AS s24 ON w.worker = s24.worker
    LEFT JOIN worker_stats_60m AS s60 ON w.worker = s60.worker
    LEFT JOIN stats_5m AS s5 ON w.worker = s5.worker
    LEFT JOIN worker_pool_latest_share_mv AS latest_share_data ON w.worker = latest_share_data.worker
    ORDER BY w.worker
    """

    result = await db.client.query(query, parameters=params)

    workers = []
    for row in result.result_rows:
        workers.append(
            {
                "worker": row[0],
                "miner": row[1],
                "last_share_ts": row[2],
                "state": row[3],
                "shares_5m": row[4] or 0,
                "hashrate_5m": row[5] or 0,
                "share_value_5m": row[6] or 0,
                "shares_60m": row[7] or 0,
                "hashrate_60m": row[8] or 0,
                "share_value_60m": row[9] or 0,
                "shares_24h": row[10] or 0,
                "hashrate_24h": row[11] or 0,
                "share_value_24h": row[12] or 0,
            }
        )
    return workers


async def get_worker_timerange_stats(
    db: StatsDB, start_time: int, end_time: int, coin: str
) -> dict[str, Any]:
    """
    Get worker statistics for a custom time range.

    Args:
        db: Database connection
        start_time: Start time as Unix timestamp
        end_time: End time as Unix timestamp
        coin: Coin name
    Returns:
        Worker statistics calculated for the specified time period.
    """
    time_diff = end_time - start_time
    if time_diff == 0:
        time_diff = 1

    try:
        start_dt = datetime.fromtimestamp(start_time)
        end_dt = datetime.fromtimestamp(end_time)

        query = """
        SELECT
            worker,
            CASE
                WHEN max(ts) > fromUnixTimestamp(%(end_time_int)s) - INTERVAL 10 MINUTE THEN 'ok'
                ELSE 'offline'
            END as state,
            toUnixTimestamp(max(ts)) as last_share,
            count() as shares,
            sum(pool_difficulty) as share_value,
            sum(pool_difficulty) * 4294967296 / %(duration)s as hashrate
        FROM shares
        WHERE ts >= %(start_time_dt)s AND ts < %(end_time_dt)s
        GROUP BY worker
        ORDER BY worker
        """

        params = {
            "start_time_dt": start_dt,
            "end_time_dt": end_dt,
            "end_time_int": end_time,
            "duration": time_diff,
        }

        result = await db.client.query(query, parameters=params)

        workers_dict = {}
        for row in result.result_rows:
            worker_name = row[0]
            workers_dict[worker_name] = {
                "state": row[1],
                "last_share": int(row[2]) if row[2] else None,
                "shares": int(row[3]),
                "share_value": float(row[4]),
                "hashrate": float(row[5]) / 1e9,
                "hash_rate_unit": "Gh/s",
            }

        # Alpha distribution %
        config_data = await get_general_config(db)
        worker_percentage = (
            config_data.get("worker_percentage", 0.0) if config_data else 0.0
        )

        return {
            coin: {"workers": workers_dict, "worker_percentage": worker_percentage}
        }

    except Exception as e:
        logger.error(f"Error fetching workers timerange data: {e}")
        raise


async def get_worker_daily_share_value(
    db: StatsDB, date: datetime.date
) -> dict[str, Any]:
    """
    Get worker share values for a specific date.

    Args:
        db: Database connection
        date: Date to query (datetime.date object)

    Returns:
        Worker statistics for the specified date from ClickHouse.
    """
    try:
        cutoff = datetime.strptime("2025-08-29", "%Y-%m-%d").date()
        if date < cutoff:
            query = """
            SELECT
                worker,
                countMerge(shares) as shares,
                sumMerge(share_value) as share_value,
                sumMerge(pool_difficulty_sum) * 4294967296 / 86400 as hashrate
            FROM worker_daily_share_value
            WHERE date = %(date)s
            GROUP BY worker
            ORDER BY worker
            """
        else:
            query = """
            SELECT
                worker,
                countMerge(shares) as shares,
                sumMerge(pool_difficulty_sum) as share_value,
                sumMerge(pool_difficulty_sum) * 4294967296 / 86400 as hashrate
            FROM worker_daily_share_value
            WHERE date = %(date)s
            GROUP BY worker
            ORDER BY worker
            """

        params = {"date": date}
        result = await db.client.query(query, parameters=params)

        workers_dict = {}
        for row in result.result_rows:
            worker_name = row[0]
            workers_dict[worker_name] = {
                "shares": int(row[1]),
                "share_value": float(row[2]),
                "hashrate": float(row[3]) / 1e9,  # Convert to GH/s
                "hash_rate_unit": "Gh/s",
            }

        return workers_dict

    except Exception as e:
        logger.error(f"Error fetching workers share value for {date}: {e}")
        raise
