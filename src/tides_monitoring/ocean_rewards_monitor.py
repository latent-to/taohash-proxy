"""Ocean-based TIDES rewards monitoring task."""

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional, Sequence
from urllib.parse import urlencode

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from src.api.services.tides_queries import calculate_custom_tides_window
from src.storage.db import StatsDB
from src.tides_monitoring.rewards_monitor import (
    load_existing_tx_hashes,
    process_tides_reward_earnings,
)
from src.utils.logger import get_logger
from src.utils.time_normalize import normalize_tides_window_snapshot, to_iso_z

logger = get_logger(__name__)

# Listed to skip as payouts already processed earlier.
SKIPPED_BLOCK_HASHES: list[str] = [
    "00000000000000000000b91e5fe8cc28925e68b0108bde52fcddd91ddde1c9b0",
    "000000000000000000005d13ff538ff3dc6958505cb2d67817a01c2ea499d9e3",
    "000000000000000000008c2e5320a18e5b1a9d9633b1f5cdc5ea01663ab86639",
    "00000000000000000000454478a3581c8085f73f6336fad9ae14507d3431612e",
    "0000000000000000000105166ece4d079c9aa3952a83c03b877944248cc7f9a9",
    "000000000000000000006872e9f841e9c8163589b01578f8ae03a7405c66afe4",
    "00000000000000000001575c2a9960b6009f4ce0f037ce5425fe42972aa86abe",
    "00000000000000000001c455a6598eaa427022b2c29c7bb9d732c26ffcb96120",
    "000000000000000000011e1dda63a40dee28496340d20def75852bcdd9dbd7aa",
    "0000000000000000000086d0f2cc55cb5ee35b2d56bd7dbd22d27657f9ee3008",
    "00000000000000000001c7b489553b870cad43a57313166ae79384436ae246b8",
    "00000000000000000000b5d4537f40a62d01487724c961d3eb45d5378d847020",
    "000000000000000000003e836e1e1ad7e4a94a743a759ecf7421475330478633",
    "00000000000000000001a4e78afec226e4dba5c6e0174c36af79062ee6f14c93",
    "0000000000000000000128225eb65351aaca98190844727dbb5b262a70ee63ae",
    "00000000000000000001f2a28dee07f9a3398629093f1d88e602d56d3ff73dd4",
    "000000000000000000017bc6267c5e83cf48f4e22ed56a75a008de4185bd6c29",
    "00000000000000000000d7c32edb112c6056dcc1521f24e801594872b22d15cb",
    "000000000000000000001e49b6179a2518a7db2ae882cceea5ee8768d9c1c206",
    "0000000000000000000116eb891d68bf2fdad711435d1f341894a942ec650942",
    "000000000000000000011cceb5c999e0e9397223f03cfb22c7c69957828b1ec6",
    "0000000000000000000041d60a92a6ed88a5f4f780ef10a467a2b56d3042f027",
]


@dataclass
class OceanEarningsRow:
    """Row representing a single Ocean earnings entry."""

    block_hash: str
    pool_percentage: float
    total_btc: float
    fee_btc: float


class _OceanEarningsHTMLParser(HTMLParser):
    """HTML parser to extract table row cell contents."""

    def __init__(self) -> None:
        super().__init__()
        self._in_row = False
        self._in_cell = False
        self._current_row: list[str] = []
        self._buffer: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(
        self, tag: str, attrs: Sequence[tuple[str, Optional[str]]]
    ) -> None:
        if tag == "tr":
            attrs_dict = dict(attrs)
            if attrs_dict.get("class") == "table-row":
                self._in_row = True
                self._current_row = []
        elif self._in_row and tag == "td":
            self._in_cell = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_row and self._in_cell:
            text = "".join(self._buffer).strip()
            if text:
                self._current_row.append(text)
            else:
                self._current_row.append("")
            self._in_cell = False
            self._buffer = []
        elif tag == "tr" and self._in_row:
            if self._current_row:
                self.rows.append(self._current_row)
            self._in_row = False
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._in_row and self._in_cell:
            self._buffer.append(data)


class OceanTemplateClient:
    """Client to fetch and parse Ocean earnings template pages."""

    def __init__(self, btc_address: str) -> None:
        self.btc_address = btc_address
        self.base_url = "https://ocean.xyz"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=5, min=1, max=5),
        reraise=True,
    )
    async def fetch_page(self, page: int) -> list[OceanEarningsRow]:
        params = {
            "user": self.btc_address,
            "epage": "1",
            "page": str(page),
            "sortParam": "",
        }
        url = f"{self.base_url}/template/workers/earnings/rows?{urlencode(params)}"

        timeout = aiohttp.ClientTimeout(total=20)
        headers = {
            "User-Agent": "taohash-ocean-monitor/1.0",
            "Accept": "text/html",
        }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(
                        "Ocean template fetch failed (status %s, page %s): %s",
                        response.status,
                        page,
                        text[:200],
                    )
                    response.raise_for_status()
                html = await response.text()

        parser = _OceanEarningsHTMLParser()
        parser.feed(html)

        rows: list[OceanEarningsRow] = []
        for raw_row in parser.rows:
            try:
                row = self._convert_row(raw_row)
            except ValueError as exc:
                logger.warning(
                    "Skipping Ocean row due to parse error: %s | %s", exc, raw_row
                )
                continue
            rows.append(row)

        return rows
