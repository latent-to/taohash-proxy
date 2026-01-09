import asyncio
import functools
import os
import sys
from datetime import datetime, timezone
from typing import Any

import toml
from aiohttp import web

from .monitoring.dashboard import create_dashboard_app
from .core.miner_session import MinerSession
from .core.pool_config import PoolConfig
from .core.command_listener import CommandListener
from .core.app_state import AppState
from .monitoring.stats import StatsManager
from .storage.db import StatsDB
from .storage.marketplace_db import MarketplaceDB
from .api.services.market_order_queries import get_order
from .api.services.market_assignment_queries import (
    get_miner_assignment,
    delete_assignment,
)
from .utils.logger import get_logger
from .utils.redis_client import RedisClient
from .utils.order_share_publisher import OrderSharePublisher
from .utils.constants import (
    RELOAD_API_PORT,
    RELOAD_API_HOST,
    INTERNAL_DASHBOARD_PORT,
    CONFIG_PATH,
)

logger = get_logger(__name__)


async def on_miner_authorized(session: MinerSession, state: AppState) -> None:
    """
    Callback invoked when a miner completes authorization.

    Registers the session in miner_registry for per-miner control.
    Also checks PostgreSQL marketplace DB if the miner has an active order assignment.
    """
    registry_key = session.get_registry_key()
    if not registry_key:
        logger.warning("Cannot register miner: no registry key available")
        return

    # Check for already existing session (reconnecting miner)
    async with state.miner_registry_lock:
        old_session = state.miner_registry.get(registry_key)
        if old_session and old_session is not session:
            logger.info(f"Miner {registry_key} reconnected, replacing old session")

        state.miner_registry[registry_key] = session
        logger.info(
            f"📝 Registered miner in registry: {registry_key} ({session.miner_agent})"
        )

    if not state.marketplace_db or not state.marketplace_db.pool:
        return

    try:
        assignment = await get_miner_assignment(state.marketplace_db.pool, registry_key)
        if not assignment:
            return

        order_id = assignment["order_id"]
        order = await get_order(state.marketplace_db.pool, order_id)

        if not order:
            # Order was deleted, clean up assignment
            logger.info(f"Order {order_id} no longer exists, deleting assignment")
            await delete_assignment(state.marketplace_db.pool, registry_key)
            return

        if order["status"] != "active":
            logger.info(
                f"⏰ Order {order_id} for miner {registry_key} is not active (status: {order['status']})"
            )
            return

        if order["balance_btc"] <= 0:
            logger.info(
                f"⏰ Order {order_id} for miner {registry_key} has no remaining balance"
            )
            return

        logger.info(
            f"🔀 Miner {registry_key} has active order {order_id}, switching to order pool"
        )

        pool_config = PoolConfig(
            host=order["host"],
            port=order["port"],
            user=order["user"],
            password=order["password"],
            label=order_id,
        )
        task = await session.request_pool_switch(pool_config)
        if not task:
            logger.warning(
                f"⚠️ Failed to request auto-switch for {registry_key} to order {order_id} "
                f"(switch already in progress)"
            )

    except Exception as e:
        logger.error(f"Error checking miner assignment: {e}")


async def unregister_miner_session(session: MinerSession, state: AppState) -> None:
    """
    Remove a miner session from the registry on disconnect.

    Note: We keep the order assignment active so the miner auto-routes on reconnect.
    """
    registry_key = session.get_registry_key()
    if not registry_key:
        return

    async with state.miner_registry_lock:
        if state.miner_registry.get(registry_key) is session:
            del state.miner_registry[registry_key]
            logger.info(f"📝 Unregistered miner from registry: {registry_key}")


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r") as f:
        data = toml.load(f)

    if "pools" not in data:
        raise ValueError("Configuration must have 'pools' section")

    return {"pools": data["pools"]}


async def update_config(state: AppState, path: str = CONFIG_PATH) -> bool:
    """
    Reload TOML → swap in new config → schedule teardown of sessions in the background.
    """
    new_conf = load_config(path)

    state.config.clear()
    state.config.update(new_conf)
    logger.info(f"🔄 Configuration reloaded: {state.config}")

    async with state.active_sessions_lock:
        sessions = list(state.active_sessions)
        state.active_sessions.clear()

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


def create_reload_handler(state: AppState):
    """Create a reload request handler with access to state."""

    async def handle_reload_request(request: web.Request) -> web.Response:
        logger.info(f"🔁 Received reload from {request.remote}")
        try:
            success = await update_config(state)
            if success:
                return web.Response(text="Reload scheduled")
            else:
                return web.Response(status=500, text="Failed to update config")
        except Exception as e:
            logger.error(f"Failed to reload configuration: {e}")
            return web.Response(status=500, text=str(e))

    return handle_reload_request


async def handle_new_miner(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: AppState,
) -> None:
    """
    Fired on each TCP connection from a miner.  We:
      1) Instantiate a MinerSession with the current pool config from state
      2) Track it in active_sessions
      3) Schedule its run() as a background task
      4) On completion, clean up stats + the session set
    """
    miner_address = writer.get_extra_info("peername")

    local_addr = writer.get_extra_info("sockname")
    local_port = local_addr[1] if local_addr else None

    pool_config = None
    pool_label = None
    for pool_name, pool_cfg in state.config["pools"].items():
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

    pool_cfg = PoolConfig.from_dict({**pool_config, "label": pool_label})
    logger.info(f"➕ Miner connected: {miner_address} → {pool_cfg.connection_string}")

    session = MinerSession(
        reader,
        writer,
        pool_cfg,
        state.stats_manager,
    )

    session.db = state.stats_db
    session.order_share_publisher = state.order_share_publisher
    session.on_authorized_callback = functools.partial(on_miner_authorized, state=state)

    async def cleanup_session() -> None:
        async with state.active_sessions_lock:
            state.active_sessions.discard(session)
        await unregister_miner_session(session, state)
        state.stats_manager.unregister_miner(miner_address)
        logger.info(f"➖ Miner disconnected: {miner_address}")

    try:
        async with state.active_sessions_lock:
            state.active_sessions.add(session)

        task = asyncio.create_task(session.run())

        def on_task_done(future: Any) -> None:
            asyncio.create_task(cleanup_session())

        task.add_done_callback(on_task_done)

    except Exception:
        await cleanup_session()
        raise


async def start_reload_api(state: AppState) -> web.TCPSite:
    """Start a separate web server just for handling reload requests (internal use only)"""
    app = web.Application()
    app.router.add_post("/api/reload", create_reload_handler(state))

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, RELOAD_API_HOST, RELOAD_API_PORT)
    await site.start()

    logger.info(
        f"Internal reload API running on http://{RELOAD_API_HOST}:{RELOAD_API_PORT}/api/reload"
    )
    return site


async def miner_heartbeat_loop(state: AppState) -> None:
    """
    Background task that publishes connected miners and their stats.

    Runs every 15 seconds, publishing miners with hashrate stats
    to Redis with a 30 second TTL.

    Handles reconnection if Redis is unavailable at startup or goes down later.
    """
    logger.info(f"Starting heartbeat loop for proxy {state.proxy_id}")

    while True:
        try:
            if not state.redis_client:
                state.redis_client = RedisClient()

            if not await state.redis_client.ensure_connected():
                logger.debug("Redis not available for heartbeat, retrying in 15s")
                await asyncio.sleep(15)
                continue

            async with state.miner_registry_lock:
                miners = {}
                for registry_key, session in state.miner_registry.items():
                    miners[registry_key] = {
                        "hashrate": session.stats.get_hashrate()
                        if session.stats
                        else 0,
                        "hashrate_gh": (session.stats.get_hashrate() / 1e9)
                        if session.stats
                        else 0,
                        "difficulty": session.stats.difficulty if session.stats else 0,
                        "accepted": session.stats.accepted if session.stats else 0,
                        "rejected": session.stats.rejected if session.stats else 0,
                        "pool_label": session.current_pool.label,
                        "worker_name": session.worker_name,
                        "miner_agent": session.stats.miner_agent
                        if session.stats
                        else "",
                        "connected_minutes": round(
                            (
                                datetime.now(timezone.utc) - session.stats.connected_at
                            ).total_seconds()
                            / 60,
                            1,
                        )
                        if session.stats
                        else 0,
                    }

            await state.redis_client.update_proxy_miners_with_stats(
                state.proxy_id, miners, ttl_seconds=30
            )

            if miners:
                logger.debug(f"Heartbeat: {len(miners)} miners published to Redis")

        except Exception as e:
            logger.error(f"Error in heartbeat loop: {e}")

        await asyncio.sleep(15)


def get_proxy_port(pool_name: str) -> int:
    if pool_name == "normal":
        return int(os.getenv("PROXY_PORT", 3331))
    elif pool_name == "high_diff":
        return int(os.getenv("PROXY_PORT_HIGH", 3332))
    else:
        return 3331


async def main() -> None:
    state = AppState()
    state.stats_manager = StatsManager()

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

                logger.info("No config.toml found; copying from example...")
                shutil.copy(example_path, config_path)
            else:
                logger.error(f"❌ Config file not found at {config_path}")
                sys.exit(1)

    await update_config(state, config_path)

    state.stats_db = StatsDB()
    db_initialized = await state.stats_db.init()
    if db_initialized:
        logger.info(
            f"✅ ClickHouse initialized at {state.stats_db.host}:{state.stats_db.port}"
        )
    else:
        logger.info("ClickHouse is disabled or not available")

    state.marketplace_db = MarketplaceDB()
    marketplace_initialized = await state.marketplace_db.init()
    if marketplace_initialized:
        logger.info(
            f"✅ PostgreSQL marketplace DB initialized at {state.marketplace_db.host}:{state.marketplace_db.port}"
        )
    else:
        logger.info("PostgreSQL marketplace DB is disabled or not available")

    state.redis_client = RedisClient()
    redis_connected = await state.redis_client.connect()
    if redis_connected:
        logger.info("✅ Redis client connected")
    else:
        logger.warning(
            "⚠️ Redis client not available, heartbeats will retry in background"
        )

    state.order_share_publisher = OrderSharePublisher(state.redis_client)
    state.order_share_publisher.start()
    logger.info("Order share publisher started")

    state.command_listener = CommandListener(
        state.miner_registry,
        state.miner_registry_lock,
        state.config,
    )
    listener_connected = await state.command_listener.connect()
    if listener_connected:
        logger.info("✅ Command listener subscribed to Redis")
        asyncio.create_task(state.command_listener.listen())
        logger.info("📡 Command listener started")
    else:
        logger.warning(
            "⚠️ Command listener not available, pool switching via Redis disabled"
        )
        state.command_listener = None

    logger.info("🚀 Starting with configuration:")
    for pool_name, pool_config in state.config["pools"].items():
        proxy_port = get_proxy_port(pool_name)
        logger.info(
            f"  {pool_name.upper()} Pool: {pool_config['host']}:{pool_config['port']}"
        )
        logger.info(f"    User: {pool_config['user']}")
        logger.info(f"    Proxy port: {proxy_port}")
    logger.info(f"  Dashboard on: 0.0.0.0:{INTERNAL_DASHBOARD_PORT}")
    logger.info(f"  Reload API on: {RELOAD_API_HOST}:{RELOAD_API_PORT} (internal only)")
    logger.info(f"  Proxy ID: {state.proxy_id}")

    await start_reload_api(state)

    asyncio.create_task(miner_heartbeat_loop(state))
    logger.info("Miner heartbeat loop started")

    handler = functools.partial(handle_new_miner, state=state)

    servers = []
    for pool_name, pool_config in state.config["pools"].items():
        proxy_port = get_proxy_port(pool_name)
        server = await asyncio.start_server(
            handler,
            "0.0.0.0",
            proxy_port,
        )
        servers.append(server)

        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info(f"🔌 {pool_name.upper()} pool proxy listening on {addrs}")

    app = create_dashboard_app(state.stats_manager, state.stats_db)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        INTERNAL_DASHBOARD_PORT,
    )
    await site.start()
    logger.info(f"Dashboard available at http://0.0.0.0:{INTERNAL_DASHBOARD_PORT}")

    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        await shutdown(state)


async def shutdown(state: AppState) -> None:
    """Properly close database connection and other resources."""
    if state.command_listener:
        await state.command_listener.close()
        logger.info("Command listener closed")

    if state.order_share_publisher:
        await state.order_share_publisher.stop()
        logger.info("Order share publisher stopped")

    if state.redis_client:
        await state.redis_client.close()
        logger.info("Redis connection closed")

    if state.marketplace_db:
        await state.marketplace_db.close()
        logger.info("PostgreSQL marketplace connection closed")

    if state.stats_db:
        await state.stats_db.close()
        logger.info("ClickHouse connection closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
