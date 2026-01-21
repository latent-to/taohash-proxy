from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class PoolProbeResult:
    ok: bool
    severity: str  # "pass" | "warn" | "fail"
    reason: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_disallowed_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return bool(
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        )
    except Exception:
        # Better to err on the safe side.
        return True


async def probe_stratum_v1(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    timeout_s: float = 3.0,
    allow_private_hosts: bool = False,
) -> PoolProbeResult:
    """Probe a Stratum V1 TCP endpoint.

    Returns PASS/WARN/FAIL.
    """

    t0 = time.time()
    details: dict[str, Any] = {
        "host": host,
        "port": port,
        "timeout_s": timeout_s,
    }

    # 1) DNS resolution (and SSRF safety)
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos})
        details["resolved_ips"] = ips
    except Exception as e:
        return PoolProbeResult(
            ok=False,
            severity="warn",
            reason="dns_resolution_failed",
            details={
                **details,
                "error": str(e),
                "took_ms": int((time.time() - t0) * 1000),
            },
        )

    allowed_ips = []
    disallowed_ips = []
    for ip in ips:
        if allow_private_hosts:
            allowed_ips.append(ip)
        else:
            if _is_disallowed_ip(ip):
                disallowed_ips.append(ip)
            else:
                allowed_ips.append(ip)

    details["allowed_ips"] = allowed_ips
    details["disallowed_ips"] = disallowed_ips

    if not allowed_ips:
        return PoolProbeResult(
            ok=False,
            severity="fail",
            reason="disallowed_ip",
            details={
                **details,
                "took_ms": int((time.time() - t0) * 1000),
                "hint": "Pool host resolved only to private/loopback/reserved addresses. "
                "Set ALLOW_PRIVATE_POOL_HOSTS=true to override (not recommended).",
            },
        )
