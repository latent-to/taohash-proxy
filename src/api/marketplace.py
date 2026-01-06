"""
Marketplace API endpoints for hashrate order management.

Provides REST API endpoints for managing marketplace orders:
- Create, list, get, and cancel orders
- Assign and remove miners from orders
- List connected miners across all proxy instances
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from src.api.models import AssignMinersRequest, CreateOrderRequest
from src.api.services.market_order_queries import (
    create_order_with_assignments,
    get_order as fetch_order,
    get_orders as fetch_orders,
    update_order_status,
)
from src.api.services.market_assignment_queries import (
    ConflictError,
    assign_miners_atomic,
    delete_assignment,
    get_miner_assignment,
    get_order_assignments,
)
from src.storage.marketplace_db import MarketplaceDB
from src.utils.logger import get_logger
from src.utils.redis_client import RedisPublisher, RedisClient
from src.utils.time_normalize import ensure_utc

logger = get_logger(__name__)

router = APIRouter(prefix="/api/marketplace", tags=["Marketplace"])

limiter = Limiter(key_func=get_remote_address)

marketplace_db: Optional[MarketplaceDB] = None
redis_publisher: Optional[RedisPublisher] = None
redis_client: Optional[RedisClient] = None


def set_dependencies(
    db: MarketplaceDB,
    publisher: RedisPublisher,
    client: RedisClient,
) -> None:
    """Set module dependencies from app startup."""
    global marketplace_db, redis_publisher, redis_client
    marketplace_db = db
    redis_publisher = publisher
    redis_client = client


# ==================== Order Endpoints ====================


@router.post("/orders")
@limiter.limit("30/minute")
async def create_order(
    request: Request,
    order_request: CreateOrderRequest,
) -> dict[str, Any]:
    """
    Create a new marketplace order.

    Optionally assign miners immediately by providing the 'miners' field.
    If any miners are already assigned to other orders, the entire request
    fails with a 409 Conflict error.

    Returns:
        Order status and assigned miners
    """
    if not marketplace_db or not marketplace_db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not redis_publisher:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=order_request.duration_seconds
    )

    miners_to_assign = order_request.miners or []

    try:
        result = await create_order_with_assignments(
            pool=marketplace_db.pool,
            order_id=order_request.order_id,
            host=order_request.host,
            port=order_request.port,
            user=order_request.user,
            password=order_request.password,
            expires_at=expires_at,
            worker_ips=miners_to_assign,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(e),
                "conflicts": e.conflicts,
                "hint": "Remove miners from their current orders first",
            },
        )

    logger.info(
        f"Created order {order_request.order_id}: {order_request.host}:{order_request.port}"
    )

    # Publish pool switches for connected miners
    assigned = []
    not_found = []

    if result["assigned"]:
        connected_miners = await redis_client.get_connected_miners()
        if not connected_miners:
            raise HTTPException(status_code=503, detail="No connected miners found")

        for worker_ip in result["assigned"]:
            if worker_ip in connected_miners:
                pool_config = {
                    "host": order_request.host,
                    "port": order_request.port,
                    "user": order_request.user,
                    "pass": order_request.password,
                    "label": order_request.order_id,
                }
                await redis_publisher.publish_switch_pool(worker_ip, pool_config)
                assigned.append(worker_ip)
                logger.info(f"Assigned {worker_ip} to order {order_request.order_id}")
            else:
                not_found.append(worker_ip)

    response = {
        "status": "created",
        "order_id": order_request.order_id,
        "expires_at": expires_at.isoformat(),
        "assigned_miners": assigned,
    }

    if miners_to_assign:
        response["message"] = (
            f"Order created, {len(assigned)} miners switching to "
            f"{order_request.host}:{order_request.port}"
        )

    if not_found:
        response["not_found"] = not_found
        response["warning"] = f"{len(not_found)} miners not currently connected"

    return response


@router.get("/orders")
@limiter.limit("60/minute")
async def list_orders(
    request: Request,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """
    List all marketplace orders.

    Query parameters:
        status: Optional filter by status ('active', 'expired', 'cancelled')

    Returns:
        List of order objects
    """
    if not marketplace_db or not marketplace_db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    orders = await fetch_orders(marketplace_db.pool, status=status)

    detailed_orders = []
    for order in orders:
        assignments = await get_order_assignments(
            marketplace_db.pool, order["order_id"]
        )
        assigned_miners = [a["worker_ip"] for a in assignments]

        remaining = None
        if order["expires_at"]:
            remaining = max(
                0,
                (
                    ensure_utc(order["expires_at"]) - datetime.now(timezone.utc)
                ).total_seconds(),
            )

        detailed_orders.append(
            {
                "order_id": order["order_id"],
                "host": order["host"],
                "port": order["port"],
                "user": order["user"],
                "expires_at": order["expires_at"].isoformat()
                if order["expires_at"]
                else None,
                "created_at": order["created_at"].isoformat()
                if order["created_at"]
                else None,
                "status": order["status"],
                "assigned_miners": assigned_miners,
                "miner_count": len(assigned_miners),
                "remaining_seconds": remaining,
            }
        )

    return {"orders": detailed_orders, "total": len(detailed_orders)}


@router.get("/orders/{order_id}")
@limiter.limit("60/minute")
async def get_order(
    request: Request,
    order_id: str,
) -> dict[str, Any]:
    """
    Get details for a specific order.

    Returns:
        Order details with assigned miners
    """
    if not marketplace_db or not marketplace_db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    order = await fetch_order(marketplace_db.pool, order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    assignments = await get_order_assignments(marketplace_db.pool, order_id)
    assigned_miners = [a["worker_ip"] for a in assignments]

    remaining = None
    if order["expires_at"]:
        remaining = max(
            0,
            (
                ensure_utc(order["expires_at"]) - datetime.now(timezone.utc)
            ).total_seconds(),
        )

    return {
        "order_id": order["order_id"],
        "host": order["host"],
        "port": order["port"],
        "user": order["user"],
        "expires_at": order["expires_at"].isoformat() if order["expires_at"] else None,
        "created_at": order["created_at"].isoformat() if order["created_at"] else None,
        "status": order["status"],
        "assigned_miners": assigned_miners,
        "miner_count": len(assigned_miners),
        "remaining_seconds": remaining,
    }


@router.delete("/orders/{order_id}")
@limiter.limit("30/minute")
async def cancel_order(
    request: Request,
    order_id: str,
) -> dict[str, Any]:
    """
    Cancel an order and switch all assigned miners back to default pool.

    Returns:
        Cancellation status and number of miners switched
    """
    if not marketplace_db or not marketplace_db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not redis_publisher:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    order = await fetch_order(marketplace_db.pool, order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    assignments = await get_order_assignments(marketplace_db.pool, order_id)

    miners_switched = 0
    for assignment in assignments:
        worker_ip = assignment["worker_ip"]

        await redis_publisher.publish_switch_default(worker_ip)
        miners_switched += 1

        await delete_assignment(marketplace_db.pool, worker_ip)

    await update_order_status(marketplace_db.pool, order_id, "cancelled")

    logger.info(f"Cancelled order {order_id}, switched {miners_switched} miners back")

    return {
        "status": "cancelled",
        "order_id": order_id,
        "miners_switched": miners_switched,
        "message": f"Order cancelled, {miners_switched} miners switched back to default pools",
    }


# ==================== Miner Assignment Endpoints ====================


@router.post("/orders/{order_id}/miners")
@limiter.limit("30/minute")
async def assign_miners(
    request: Request,
    order_id: str,
    assign_request: AssignMinersRequest,
) -> dict[str, Any]:
    """
    Assign miners to an existing order.

    Miners already assigned to THIS order are skipped (idempotent).
    If any miners are assigned to OTHER orders, the entire request
    fails with a 409 Conflict error.

    Returns:
        Assignment status with list of assigned miners
    """
    if not marketplace_db or not marketplace_db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not redis_publisher:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    try:
        result = await assign_miners_atomic(
            pool=marketplace_db.pool,
            order_id=order_id,
            worker_ips=assign_request.miners,
        )
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=410, detail=str(e))
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(e),
                "conflicts": e.conflicts,
                "hint": "Remove miners from their current orders first",
            },
        )

    # Publish pool switches for newly assigned connected miners
    assigned = []
    not_found = []

    if result["assigned"]:
        connected_miners = (
            await redis_client.get_connected_miners() if redis_client else []
        )
        connected_set = set(connected_miners)

        order = await fetch_order(marketplace_db.pool, order_id)

        for worker_ip in result["assigned"]:
            if worker_ip in connected_set:
                pool_config = {
                    "host": order["host"],
                    "port": order["port"],
                    "user": order["user"],
                    "pass": order["password"],
                    "label": order_id,
                }
                await redis_publisher.publish_switch_pool(worker_ip, pool_config)
                assigned.append(worker_ip)
                logger.info(f"Assigned {worker_ip} to order {order_id}")
            else:
                not_found.append(worker_ip)

    all_assignments = await get_order_assignments(marketplace_db.pool, order_id)

    response = {
        "status": "assigned",
        "order_id": order_id,
        "newly_assigned": assigned,
        "skipped": result["skipped"],
        "total_miners": len(all_assignments),
    }

    if not_found:
        response["not_found"] = not_found
        response["warning"] = f"{len(not_found)} miners not currently connected"

    return response


@router.delete("/orders/{order_id}/miners/{worker_ip:path}")
@limiter.limit("30/minute")
async def remove_miner(
    request: Request,
    order_id: str,
    worker_ip: str,
) -> dict[str, Any]:
    """
    Remove a miner from an order and switch back to default pool.

    The worker_ip path parameter should be URL-encoded (e.g., worker1%3A192.168.1.100).

    Returns:
        Removal status
    """
    if not marketplace_db or not marketplace_db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not redis_publisher:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    order = await fetch_order(marketplace_db.pool, order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    assignment = await get_miner_assignment(marketplace_db.pool, worker_ip)
    if not assignment or assignment["order_id"] != order_id:
        raise HTTPException(
            status_code=404,
            detail=f"Miner {worker_ip} not assigned to order {order_id}",
        )

    await delete_assignment(marketplace_db.pool, worker_ip)

    await redis_publisher.publish_switch_default(worker_ip)

    logger.info(f"Removed {worker_ip} from order {order_id}")

    return {
        "status": "removed",
        "order_id": order_id,
        "worker_ip": worker_ip,
        "message": "Miner switched back to default pool",
    }


# ==================== Miner Listing Endpoint ====================


@router.get("/miners")
@limiter.limit("60/minute")
async def list_miners(
    request: Request,
) -> dict[str, Any]:
    """
    List all currently connected miners across all proxy instances.

    Returns miner IDs with their current order assignment (if any).

    Returns:
        List of connected miners with assignment info
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    connected_miners = await redis_client.get_connected_miners()

    miners = []
    for worker_ip in connected_miners:
        assignment = (
            await get_miner_assignment(marketplace_db.pool, worker_ip)
            if marketplace_db and marketplace_db.pool
            else None
        )
        miners.append(
            {
                "worker_ip": worker_ip,
                "order_id": assignment["order_id"] if assignment else None,
            }
        )

    return {"miners": miners, "count": len(miners)}


@router.get("/miners/stats")
@limiter.limit("60/minute")
async def get_miners_stats(
    request: Request,
) -> dict[str, Any]:
    """
    Get hashrate stats for all connected miners.

    Returns miners with their real-time hashrate and pool assignment.
    Used by marketplace to make allocation decisions.

    Returns:
        List of miners sorted by hashrate (descending)
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    stats = await redis_client.get_all_miners_with_stats()

    sorted_miners = sorted(
        stats.items(), key=lambda x: x[1].get("hashrate", 0), reverse=True
    )

    miners = []
    total_hashrate = 0
    available_hashrate = 0

    for worker_ip, miner_stats in sorted_miners:
        hashrate_gh = miner_stats.get("hashrate_gh", 0)
        pool_label = miner_stats.get("pool_label", "unknown")
        is_available = pool_label in ("normal", "high_diff")

        total_hashrate += hashrate_gh
        if is_available:
            available_hashrate += hashrate_gh

        miners.append(
            {
                "worker_ip": worker_ip,
                "hashrate": miner_stats.get("hashrate", 0),
                "hashrate_gh": hashrate_gh,
                "difficulty": miner_stats.get("difficulty", 0),
                "accepted": miner_stats.get("accepted", 0),
                "rejected": miner_stats.get("rejected", 0),
                "pool_label": pool_label,
                "worker_name": miner_stats.get("worker_name"),
                "available": is_available,
                "miner_agent": miner_stats.get("miner_agent", ""),
                "connected_minutes": miner_stats.get("connected_minutes", 0),
            }
        )

    return {
        "miners": miners,
        "total_miners": len(miners),
        "total_hashrate_gh": total_hashrate,
        "available_hashrate_gh": available_hashrate,
    }
