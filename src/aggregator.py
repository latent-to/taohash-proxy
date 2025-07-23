import asyncio
import os
import time

import aiohttp
import toml
from aiohttp import web


class StatsAggregator:
    def __init__(self):
        self.cached_stats = []
        self.last_update = 0
        self.cache_duration = 1

    async def fetch_all_stats(self) -> list[dict]:
        """Fetch stats from all proxy instances"""
        all_stats = []

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=0.5)
        ) as session:
            tasks = []
            for i in range(1, 13):
                url = f"http://docker-proxy-{i}:5000/api/stats"
                tasks.append(self._fetch_one(session, url))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    all_stats.extend(result)

        return all_stats

    async def _fetch_one(self, session, url):
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
        except:
            pass
        return []

    async def get_stats(self):
        """Get stats with caching"""
        now = time.time()
        if now - self.last_update > self.cache_duration:
            self.cached_stats = await self.fetch_all_stats()
            self.last_update = now
        return self.cached_stats


def create_app():
    app = web.Application()
    aggregator = StatsAggregator()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    static_dir = os.path.join(project_root, "static")
    config_dir = os.path.join(project_root, "config")

    async def index_handler(request):
        """Serve the static index.html"""
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        else:
            return web.Response(text="index.html not found", status=404)

    async def api_stats_handler(request):
        """API endpoint that the HTML's JavaScript will call"""
        stats = await aggregator.get_stats()
        return web.json_response(stats)

    async def api_pools_handler(request):
        """API endpoint for pools info - REQUIRED by dashboard.js"""
        pools_data = {}

        try:
            config_path = os.path.join(config_dir, "config.toml")
            if os.path.exists(config_path):
                data = toml.load(config_path)
                if "pools" in data:
                    stats = await aggregator.get_stats()

                    for pool_name, pool_config in data["pools"].items():
                        if pool_name == "normal":
                            proxy_port = int(os.getenv("PROXY_PORT", "3331"))
                        elif pool_name == "high_diff":
                            proxy_port = int(os.getenv("PROXY_PORT_HIGH", "3332"))
                        else:
                            proxy_port = 3331

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

                    for miner in stats:
                        pool_name = miner.get("pool", "unknown")
                        if pool_name in pools_data:
                            pools_data[pool_name]["connected_miners"] += 1
                            pools_data[pool_name]["total_hashrate"] += miner.get(
                                "hashrate", 0
                            )
                            pools_data[pool_name]["total_accepted"] += miner.get(
                                "accepted", 0
                            )
                            pools_data[pool_name]["total_rejected"] += miner.get(
                                "rejected", 0
                            )
        except Exception as e:
            print(f"Error reading pools info: {e}")

        return web.json_response(pools_data)

    app.router.add_get("/", index_handler)
    app.router.add_get("/api/stats", api_stats_handler)
    app.router.add_get("/api/pools", api_pools_handler)

    app.router.add_static("/static/", static_dir)

    return app


async def main():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 5555)
    await site.start()
    print("Aggregated dashboard running on http://0.0.0.0:5555")
    print("The dashboard will show miners from ALL proxy instances")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
