#!/usr/bin/env python3
"""
Daily snapshot script for worker share values.

Run this script daily at 00:05 UTC to capture the complete previous day's data.
Can be run manually to backfill historical data.
"""

import os
import sys
import json
import aiosqlite
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)

API_URL = os.environ.get("API_URL", "http://localhost:8888")
API_TOKEN = os.environ.get("API_TOKEN", "")
HISTORICAL_DB_PATH = os.environ.get("HISTORICAL_DB_PATH", "/data/historical/share_values.db")


async def fetch_day_data(date: datetime.date) -> Optional[dict]:
    """Fetch worker data for a specific UTC date."""
    start_dt = datetime.combine(date, datetime.min.time(), timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    
    start_time = int(start_dt.timestamp())
    end_time = int(end_dt.timestamp())
    
    url = f"{API_URL}/api/workers/timerange?start_time={start_time}&end_time={end_time}"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    
    logger.info(f"Fetching data for {date} from {url}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Successfully fetched data for {date}: {len(data.get('btc', {}).get('workers', {}))} workers")
                    return data
                else:
                    logger.error(f"API returned status {response.status}: {await response.text()}")
                    return None
        except Exception as e:
            logger.error(f"Failed to fetch data for {date}: {e}")
            return None


async def save_snapshot(date: datetime.date, data: dict) -> bool:
    """Save snapshot to SQLite database."""
    date_str = date.strftime("%Y-%m-%d")
    
    try:
        os.makedirs(os.path.dirname(HISTORICAL_DB_PATH), exist_ok=True)
        
        async with aiosqlite.connect(HISTORICAL_DB_PATH) as conn:
            # Create table if not exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    date TEXT PRIMARY KEY,
                    data JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Insert or replace snapshot
            await conn.execute(
                "INSERT OR REPLACE INTO daily_snapshots (date, data) VALUES (?, ?)",
                (date_str, json.dumps(data))
            )
            await conn.commit()
            
        logger.info(f"Successfully saved snapshot for {date_str}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to save snapshot for {date_str}: {e}")
        return False


async def capture_yesterday():
    """Capture yesterday's complete data."""
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    
    logger.info(f"Starting snapshot for {yesterday}")
    
    data = await fetch_day_data(yesterday)
    if data:
        if await save_snapshot(yesterday, data):
            logger.info(f"Snapshot completed for {yesterday}")
            return True
        else:
            logger.error(f"Failed to save snapshot for {yesterday}")
            return False
    else:
        logger.error(f"No data received for {yesterday}")
        return False


async def backfill_dates(start_date: datetime.date, end_date: datetime.date):
    """Backfill historical data for a date range."""
    current_date = start_date
    success_count = 0
    fail_count = 0
    
    while current_date <= end_date:
        logger.info(f"Backfilling {current_date}")
        
        data = await fetch_day_data(current_date)
        if data and await save_snapshot(current_date, data):
            success_count += 1
        else:
            fail_count += 1
        
        current_date += timedelta(days=1)
        
        await asyncio.sleep(1)
    
    logger.info(f"Backfill complete. Success: {success_count}, Failed: {fail_count}")


async def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Capture daily worker share value snapshots")
    parser.add_argument("--yesterday", action="store_true", help="Capture yesterday's data (default)")
    parser.add_argument("--date", type=str, help="Capture specific date (YYYY-MM-DD)")
    parser.add_argument("--backfill", type=str, help="Backfill date range (YYYY-MM-DD:YYYY-MM-DD)")
    
    args = parser.parse_args()
    
    if not API_TOKEN:
        logger.error("API_TOKEN environment variable is required")
        sys.exit(1)
    
    if args.date:
        # Specific date
        date = datetime.strptime(args.date, "%Y-%m-%d").date()
        data = await fetch_day_data(date)
        if data:
            await save_snapshot(date, data)
    elif args.backfill:
        # Date range
        start_str, end_str = args.backfill.split(":")
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        await backfill_dates(start_date, end_date)
    else:
        # Default: yesterday
        await capture_yesterday()


if __name__ == "__main__":
    asyncio.run(main())