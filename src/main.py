import asyncio
import os
import sys
from typing import Any, Optional

import toml
from aiohttp import web

from .monitoring.dashboard import create_dashboard_app
from .core.miner_session import MinerSession
from .monitoring.stats import StatsManager
from .storage.db import StatsDB
from .utils.logger import get_logger
from .utils.constants import (
    RELOAD_API_PORT,
    RELOAD_API_HOST,
    PROXY_PORT,
    INTERNAL_DASHBOARD_PORT,
    CONFIG_PATH,
)

logger = get_logger(__name__)

config: dict = {}
active_sessions: set[MinerSession] = set()
active_sessions_lock = asyncio.Lock()
stats_manager = StatsManager()
stats_db: Optional[StatsDB] = None


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r") as f:
        data = toml.load(f)

    if "pools" not in data:
        raise ValueError("Configuration must have 'pools' section")

    return {"pools": data["pools"]}


async def update_config(path: str = CONFIG_PATH) -> bool:
    """
    Reload TOML → swap in new `config` → schedule teardown of sessions in the background.
    """
    new_conf = load_config(path)

    config.clear()
    config.update(new_conf)
    logger.info(f"🔄 Configuration reloaded: {config}")

    async with active_sessions_lock:
        sessions = list(active_sessions)
        active_sessions.clear()

    async def close_sessions_background() -> None:
        for sess in sessions:
            try:
                sess.miner_writer.transport.abort()
                if hasattr(sess, "pool_session") and sess.pool_session:
                    sess.pool_session.writer.transport.abort()
            except Exception as e:
                logger.error(f"⚠️ Error closing session: {e}")
        logger.info(
            f"🔌 Scheduled closure of {len(sessions)} sessions. New connections will use updated config."
        )

    asyncio.create_task(close_sessions_background())

    return True


async def handle_reload_request(request: web.Request) -> web.Response:
    logger.info(f"🔁 Received reload from {request.remote}")
    try:
        success = await update_config()
        if success:
            return web.Response(text="Reload scheduled")
        else:
            return web.Response(status=500, text="Failed to update config")
    except Exception as e:
        logger.error(f"Failed to reload configuration: {e}")
        return web.Response(status=500, text=str(e))


async def handle_new_miner(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """
    Fired on each TCP connection from a miner.  We:
      1) Instantiate a MinerSession with the current pool config from global config
      2) Track it in active_sessions
      3) Schedule its run() as a background task
      4) On completion, clean up stats + the session set
    """
    miner_address = writer.get_extra_info("peername")

    local_addr = writer.get_extra_info("sockname")
    local_port = local_addr[1] if local_addr else None

    pool_config = None
    pool_label = None
    for pool_name, pool_cfg in config["pools"].items():
        expected_port = get_proxy_port(pool_name)
        if expected_port == local_port:
            pool_config = pool_cfg
            pool_label = pool_name
            break

    if not pool_config:
        logger.error(f"❌ No pool config found for port {local_port}")
        writer.close()
        await writer.wait_closed()
        return

    pool_name = f"{pool_config['host']}:{pool_config['port']}"
    logger.info(f"➕ Miner connected: {miner_address} → {pool_name}")

    session = MinerSession(
        reader,
        writer,
        pool_config["host"],
        pool_config["port"],
        pool_config["user"],
        pool_config["pass"],
        stats_manager,
        pool_label,
    )

    session.db = stats_db

    async def cleanup_session() -> None:
        async with active_sessions_lock:
            active_sessions.discard(session)
        stats_manager.unregister_miner(miner_address)
        logger.info(f"➖ Miner disconnected: {miner_address}")

    try:
        async with active_sessions_lock:
            active_sessions.add(session)

        task = asyncio.create_task(session.run())

        def on_task_done(future: Any) -> None:
            asyncio.create_task(cleanup_session())

        task.add_done_callback(on_task_done)

    except Exception:
        await cleanup_session()
        raise


async def start_reload_api() -> web.TCPSite:
    """Start a separate web server just for handling reload requests (internal use only)"""
    app = web.Application()
    app.router.add_post("/api/reload", handle_reload_request)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, RELOAD_API_HOST, RELOAD_API_PORT)
    await site.start()

    logger.info(
        f"Internal reload API running on http://{RELOAD_API_HOST}:{RELOAD_API_PORT}/api/reload"
    )
    return site


def get_proxy_port(pool_name: str) -> int:
    if pool_name == "normal":
        return int(os.getenv("PROXY_PORT", 3331))
    elif pool_name == "high_diff":
        return int(os.getenv("PROXY_PORT_HIGH", 3332))
    else:
        return 3331


async def main() -> None:
    if os.path.exists(CONFIG_PATH):
        config_path = CONFIG_PATH
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(script_dir)
        config_path = os.path.join(parent_dir, "config", "config.toml")
        if not os.path.exists(config_path):
            example_path = os.path.join(parent_dir, "config", "config.toml.example")
            if os.path.exists(example_path):
                import shutil

                logger.info("📋 No config.toml found; copying from example...")
                shutil.copy(example_path, config_path)
            else:
                logger.error(f"❌ Config file not found at {config_path}")
                sys.exit(1)

    await update_config(config_path)

    global stats_db
    stats_db = StatsDB()

    db_initialized = await stats_db.init()
    if db_initialized:
        logger.info(f"✅ ClickHouse initialized at {stats_db.host}:{stats_db.port}")
    else:
        logger.info("ℹ️ ClickHouse is disabled or not available")

    logger.info("🚀 Starting with configuration:")
    for pool_name, pool_config in config["pools"].items():
        proxy_port = get_proxy_port(pool_name)
        logger.info(
            f"  {pool_name.upper()} Pool: {pool_config['host']}:{pool_config['port']}"
        )
        logger.info(f"    User: {pool_config['user']}")
        logger.info(f"    Proxy port: {proxy_port}")
    logger.info(f"  Dashboard on: 0.0.0.0:{INTERNAL_DASHBOARD_PORT}")
    logger.info(f"  Reload API on: {RELOAD_API_HOST}:{RELOAD_API_PORT} (internal only)")

    await start_reload_api()

    # Start a server for each pool configuration
    servers = []
    for pool_name, pool_config in config["pools"].items():
        proxy_port = get_proxy_port(pool_name)
        server = await asyncio.start_server(
            handle_new_miner,
            "0.0.0.0",
            proxy_port,
        )
        servers.append(server)

        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info(f"🔌 {pool_name.upper()} pool proxy listening on {addrs}")

    app = create_dashboard_app(stats_manager, stats_db)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        INTERNAL_DASHBOARD_PORT,
    )
    await site.start()
    logger.info(f"✅ Dashboard available at http://0.0.0.0:{INTERNAL_DASHBOARD_PORT}")

    await asyncio.gather(*(server.serve_forever() for server in servers))


async def shutdown():
    """Properly close database connection and other resources."""
    global stats_db
    if stats_db:
        await stats_db.close()
        logger.info("Database connection closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        try:
            asyncio.run(shutdown())
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
