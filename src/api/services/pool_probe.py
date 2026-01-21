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

    # 2) TCP connect (try the first allowed IP)
    ip = allowed_ips[0]
    details["connect_ip"] = ip

    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout_s
        )
    except Exception as e:
        return PoolProbeResult(
            ok=False,
            severity="warn",
            reason="connect_failed",
            details={
                **details,
                "error": str(e),
                "took_ms": int((time.time() - t0) * 1000),
            },
        )

    # 3) Best-effort Stratum handshake: subscribe + authorize
    try:
        subscribe = {"id": 1, "method": "mining.subscribe", "params": []}
        authorize = {"id": 2, "method": "mining.authorize", "params": [user, password]}
        writer.write((json.dumps(subscribe) + "\n").encode())
        writer.write((json.dumps(authorize) + "\n").encode())
        await writer.drain()

        got_any = False
        auth_result: Optional[bool] = None
        auth_error: Optional[Any] = None
        methods: set[str] = set()

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            got_any = True
            try:
                msg = json.loads(line.decode(errors="ignore").strip())
            except Exception:
                continue

            if isinstance(msg, dict):
                if msg.get("method"):
                    methods.add(str(msg.get("method")))
                if msg.get("id") == 2:
                    auth_result = msg.get("result")
                    auth_error = msg.get("error")
                    break

        details["got_any_response"] = got_any
        details["observed_methods"] = sorted(methods)
        details["authorize_result"] = auth_result
        details["authorize_error"] = auth_error
        details["took_ms"] = int((time.time() - t0) * 1000)

        if auth_result is True and not auth_error:
            return PoolProbeResult(
                ok=True, severity="pass", reason="authorize_ok", details=details
            )

        if auth_result is False or auth_error:
            return PoolProbeResult(
                ok=False,
                severity="fail",
                reason="authorize_rejected",
                details={
                    **details,
                    "hint": "Pool rejected credentials; mining would likely be wasted.",
                },
            )

        # No explicit auth response. If we saw notify/difficulty, likely OK.
        if methods.intersection({"mining.notify", "mining.set_difficulty"}):
            return PoolProbeResult(
                ok=True, severity="warn", reason="no_auth_but_jobs", details=details
            )

        if got_any:
            return PoolProbeResult(
                ok=True, severity="warn", reason="partial_handshake", details=details
            )

        return PoolProbeResult(
            ok=False, severity="warn", reason="no_response", details=details
        )

    finally:
        try:
            if writer:
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass
