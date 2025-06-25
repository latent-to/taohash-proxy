"""
Real-time monitoring dashboard for the mining proxy.

Provides a web interface to monitor miner connections, hashrates, and share statistics.
Includes both a visual HTML dashboard and JSON API endpoints for programmatic access.

Key features:
- Live miner statistics (hashrate, shares, difficulty)
- Pool connection status monitoring
- Per-worker performance metrics
- Optional analytics integration with ClickHouse
"""

import os
import toml
from typing import Union
from aiohttp import web

from ..utils.logger import get_logger
from .stats import StatsManager

logger = get_logger(__name__)


def create_dashboard_app(stats_manager: StatsManager, stats_db=None) -> web.Application:
    """
    Create the web application for monitoring mining proxy operations.

    Sets up routes for the dashboard UI and API endpoints. Integrates with
    optional ClickHouse analytics when database is provided.

    Args:
        stats_manager: Provides real-time miner statistics
        stats_db: Optional ClickHouse connection for historical analytics

    Returns:
        Configured aiohttp application with dashboard and API routes
    """
    app = web.Application()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Go up two levels: monitoring -> src -> project root
    src_dir = os.path.dirname(base_dir)
    project_root = os.path.dirname(src_dir)
    static_dir = os.path.join(project_root, "static")

    pool_info: dict[str, Union[str, int]] = {
        "url": "Not connected",
        "user": "N/A",
        "connected_since": 0,
    }

    async def index(request: web.Request) -> web.Response:
        """
        Serve the main dashboard interface.

        Returns an HTML page with real-time charts and statistics
        for monitoring miner performance and pool connectivity.
        """
        index_path = os.path.join(static_dir, "index.html")
        return web.FileResponse(index_path)

    async def api_stats(request: web.Request) -> web.Response:
        """
        Return real-time mining statistics for all connected workers.

        Response includes per-miner data:
        - Current hashrate (5m/60m/24h averages)
        - Accepted/rejected share counts
        - Current difficulty
        - Connection uptime
        - Pool assignments

        Returns:
            JSON object with miner addresses as keys
        """
        stats = stats_manager.get_all_stats()
        return web.json_response(stats)

    async def api_pool_info(request: web.Request) -> web.Response:
        """
        Return current mining pool configuration.

        Reads pool settings from config.toml and returns:
        - Pool URL (host:port)
        - Mining username/wallet address
        - Connection timestamp

        Used by dashboard to display pool connectivity status.

        Returns:
            JSON with {url, user, connected_since}
        """
        try:
            config_dir = os.path.join(project_root, "config")
            config_path = os.path.join(config_dir, "config.toml")

            if os.path.exists(config_path):
                data = toml.load(config_path)

                if "pool" in data:
                    pool = data["pool"]

                    pool_info["url"] = (
                        f"{pool.get('host', 'unknown')}:{pool.get('port', '0')}"
                    )
                    pool_info["user"] = pool.get("user", "N/A")
        except Exception as e:
            logger.error(f"Error reading pool info: {e}")

        return web.json_response(pool_info)

    async def api_pools_info(request: web.Request) -> web.Response:
        """
        Return all mining pools with configuration and live statistics.

        Combines pool configuration from config.toml with real-time
        statistics from connected miners.

        Returns:
            JSON with pool names as keys, each containing:
            - host, port, proxy_port from config
            - connected_miners count
            - total_hashrate sum
            - total accepted/rejected shares
        """
        try:
            config_dir = os.path.join(project_root, "config")
            config_path = os.path.join(config_dir, "config.toml")

            pools_data = {}

            if os.path.exists(config_path):
                data = toml.load(config_path)
                if "pools" in data:
                    for pool_name, pool_config in data["pools"].items():
                        if pool_name == "normal":
                            proxy_port = int(os.getenv("PROXY_PORT", "3331"))
                        elif pool_name == "high_diff":
                            proxy_port = int(os.getenv("PROXY_PORT_HIGH", "3332"))

                        pools_data[pool_name] = {
                            "host": pool_config.get("host", "unknown"),
                            "port": pool_config.get("port", 0),
                            "proxy_port": proxy_port,
                            "user": pool_config.get("user", "unknown"),
                            "connected_miners": 0,
                            "total_hashrate": 0.0,
                            "total_accepted": 0,
                            "total_rejected": 0,
                        }

            miner_stats = stats_manager.get_all_stats()
            for miner in miner_stats:
                pool_name = miner.get("pool", "unknown")
                if pool_name in pools_data:
                    pools_data[pool_name]["connected_miners"] += 1
                    pools_data[pool_name]["total_hashrate"] += miner.get("hashrate", 0)
                    pools_data[pool_name]["total_accepted"] += miner.get("accepted", 0)
                    pools_data[pool_name]["total_rejected"] += miner.get("rejected", 0)

            return web.json_response(pools_data)

        except Exception as e:
            logger.error(f"Error reading pools info: {e}")
            return web.json_response({})

    app.router.add_static("/static/", static_dir)

    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/stats", api_stats),
            web.get("/api/pool", api_pool_info),
            web.get("/api/pools", api_pools_info),
        ]
    )

    # Add analytics routes if database is available
    if stats_db:
        from .analytics_api import create_analytics_routes

        # Disabled temporarily
        # create_analytics_routes(app, stats_db)

    return app
