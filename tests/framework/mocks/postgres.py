from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from tests.framework.chaos import FaultPlan
from tests.framework.clock import DeterministicClock


class _Transaction:
    """No-op async transaction context manager."""

    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _AcquireContext:
    def __init__(self, conn: "InMemoryAsyncPGConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "InMemoryAsyncPGConnection":
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@dataclass
class MarketplaceStore:
    """In-memory representation of the marketplace tables used by the allocator."""

    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    assignments: list[dict[str, Any]] = field(default_factory=list)

    def active_assignment_for(self, miner_id: str) -> Optional[dict[str, Any]]:
        for row in self.assignments:
            if row.get("miner_id") == miner_id and row.get("status") == "active":
                return row
        return None

    def active_assignments_for_order(self, order_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.assignments
            if row.get("order_id") == order_id and row.get("status") == "active"
        ]


class InMemoryAsyncPGConnection:
    """Asyncpg connection stub."""

    def __init__(
        self,
        *,
        store: MarketplaceStore,
        clock: DeterministicClock,
        faults: FaultPlan,
    ) -> None:
        self._store = store
        self._clock = clock
        self._faults = faults

    def transaction(self) -> _Transaction:
        return _Transaction()

    def _now(self) -> datetime:
        return self._clock.now()

    async def fetchval(self, query: str, *args: Any) -> Any:
        await self._faults.before("db.fetchval", self._clock)
        query = " ".join(str(query).split()).lower()

        if "pg_try_advisory_lock" in query:
            return True

        if "insert into miner_assignments" in query and "returning assignment_id" in query:
            miner_id = str(args[0])
            order_id = str(args[1])

            if self._store.active_assignment_for(miner_id) is not None:
                raise asyncpg.UniqueViolationError("active assignment already exists")

            assignment_id = str(uuid.uuid4())
            now = self._now()
            self._store.assignments.append(
                {
                    "assignment_id": assignment_id,
                    "miner_id": miner_id,
                    "order_id": order_id,
                    "status": "active",
                    "assigned_at": now,
                    "unassigned_at": None,
                    "updated_at": now,
                }
            )
            return assignment_id

        if "select version" in query:
            return "in-memory-postgres"

        raise NotImplementedError(
            f"InMemoryAsyncPGConnection.fetchval cannot handle query: {query!r}"
        )

    async def execute(self, query: str, *args: Any) -> str:
        await self._faults.before("db.execute", self._clock)
        query = " ".join(str(query).split()).lower()

        if (
            "update miner_assignments" in query
            and "where miner_id" in query
            and "status = 'active'" in query
        ):
            miner_id = str(args[0])
            updated = 0
            now = self._now()
            for row in self._store.assignments:
                if row.get("miner_id") == miner_id and row.get("status") == "active":
                    row["status"] = "completed"
                    row["unassigned_at"] = now
                    row["updated_at"] = now
                    updated += 1
            return f"UPDATE {updated}"

        if "insert into marketplace_orders" in query:
            # Matches create_order()
            order_id = str(args[0])
            host = str(args[1])
            port = int(args[2])
            username = str(args[3])
            password = str(args[4])
            price = Decimal(str(args[5]))
            amount = Decimal(str(args[6]))
            limit = args[7] if len(args) >= 8 else None

            now = self._now()
            self._store.orders[order_id] = {
                "order_id": order_id,
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "price_btc_eh_day": price,
                "amount_btc": amount,
                "balance_btc": amount,
                "limit_eh_s": limit,
                "status": "active",
                "created_at": now,
                "updated_at": now,
                "queue_position_time": now,
                "fulfilled_at": None,
                "paused_at": None,
                "cancelled_at": None,
                "last_rebalance_at": None,
                "total_shares_credited": 0,
                "total_share_value_credited": Decimal("0"),
                "last_share_credited_at": None,
            }
            return "INSERT 0 1"

        # UPDATE marketplace_orders SET status = $1 ... WHERE order_id = $2
        if "update marketplace_orders" in query and "where order_id" in query:
            status = str(args[0])
            order_id = str(args[1])
            row = self._store.orders.get(order_id)
            if not row:
                return "UPDATE 0"

            now = self._now()
            row["status"] = status
            row["updated_at"] = now
            if "cancelled_at" in query:
                row["cancelled_at"] = now
            if "fulfilled_at" in query:
                row["fulfilled_at"] = now
            if "paused_at" in query:
                row["paused_at"] = now if status == "paused" else None
            if "queue_position_time" in query and status == "active":
                row["queue_position_time"] = now

            return "UPDATE 1"

        raise NotImplementedError(
            f"InMemoryAsyncPGConnection.execute cannot handle query: {query!r}"
        )

    async def fetchrow(self, query: str, *args: Any) -> Optional[dict[str, Any]]:
        await self._faults.before("db.fetchrow", self._clock)
        query = " ".join(str(query).split()).lower()

        if "from marketplace_orders" in query and "where order_id" in query:
            order_id = str(args[0])
            row = self._store.orders.get(order_id)
            return dict(row) if row else None

        if (
            "from miner_assignments" in query
            and "where miner_id" in query
            and "status = 'active'" in query
        ):
            miner_id = str(args[0])
            row = self._store.active_assignment_for(miner_id)
            return dict(row) if row else None

        raise NotImplementedError(
            f"InMemoryAsyncPGConnection.fetchrow cannot handle query: {query!r}"
        )

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        await self._faults.before("db.fetch", self._clock)
        query = " ".join(str(query).split()).lower()

        if "from marketplace_orders" in query:
            rows = list(self._store.orders.values())
            if "where status" in query:
                status = str(args[0])
                rows = [r for r in rows if r.get("status") == status]

            # Mimic ORDER BY created_at DESC
            rows = sorted(
                rows,
                key=lambda r: r.get("created_at")
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return [dict(r) for r in rows]

        if (
            "from miner_assignments" in query
            and "where order_id" in query
            and "status = 'active'" in query
        ):
            order_id = str(args[0])
            rows = self._store.active_assignments_for_order(order_id)
            return [dict(r) for r in rows]

        raise NotImplementedError(
            f"InMemoryAsyncPGConnection.fetch cannot handle query: {query!r}"
        )


class InMemoryAsyncPGPool:
    """A tiny asyncpg pool stub backed by a MarketplaceStore."""

    def __init__(
        self,
        *,
        store: MarketplaceStore,
        clock: DeterministicClock,
        faults: Optional[FaultPlan] = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._faults = faults or FaultPlan()
        self._conn = InMemoryAsyncPGConnection(
            store=store, clock=clock, faults=self._faults
        )

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


class InMemoryMarketplaceDB:
    """Drop-in replacement for `src.storage.marketplace_db.MarketplaceDB` in tests."""

    def __init__(
        self,
        *,
        clock: DeterministicClock,
        faults: Optional[FaultPlan] = None,
    ) -> None:
        self.clock = clock
        self.faults = faults or FaultPlan()
        self.store = MarketplaceStore()
        self.pool = InMemoryAsyncPGPool(
            store=self.store, clock=self.clock, faults=self.faults
        )

    # Convenience helpers
    def upsert_order(
        self,
        *,
        order_id: str,
        host: str = "pool.example",
        port: int = 3333,
        username: str = "user",
        password: str = "x",
        price_btc_eh_day: Decimal = Decimal("1"),
        amount_btc: Decimal = Decimal("1"),
        balance_btc: Optional[Decimal] = None,
        limit_eh_s: Optional[Decimal] = None,
        status: str = "active",
        queue_position_time: Optional[datetime] = None,
    ) -> None:
        now = self.clock.now()
        self.store.orders[order_id] = {
            "order_id": str(order_id),
            "host": str(host),
            "port": int(port),
            "username": str(username),
            "password": str(password),
            "price_btc_eh_day": Decimal(str(price_btc_eh_day)),
            "amount_btc": Decimal(str(amount_btc)),
            "balance_btc": Decimal(
                str(balance_btc if balance_btc is not None else amount_btc)
            ),
            "limit_eh_s": limit_eh_s,
            "status": str(status),
            "created_at": now,
            "updated_at": now,
            "queue_position_time": queue_position_time or now,
            "fulfilled_at": None,
            "paused_at": None,
            "cancelled_at": None,
            "last_rebalance_at": None,
            "total_shares_credited": 0,
            "total_share_value_credited": Decimal("0"),
            "last_share_credited_at": None,
        }

    def active_assignment(self, miner_id: str) -> Optional[str]:
        row = self.store.active_assignment_for(str(miner_id))
        return str(row.get("order_id")) if row else None

    def active_assignments_by_miner(self) -> dict[str, dict[str, Any]]:
        """Return active assignments keyed by miner_id.
        """
        output: dict[str, dict[str, Any]] = {}
        for row in self.store.assignments:
            if row.get("status") != "active":
                continue
            miner_id = str(row.get("miner_id"))
            output[miner_id] = dict(row)
        return output
