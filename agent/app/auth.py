"""Identity via Tailscale.

The agent listens on 127.0.0.1 only; the sole way in from the network is
`tailscale serve`, which proxies to it and normally adds Tailscale-User-Login /
Tailscale-User-Name headers identifying the tailnet user.

Those headers can be missing (e.g. for users the machine was *shared* with
from another tailnet). Then we take the client's Tailscale IP from
X-Forwarded-For (set by tailscale serve) and ask the local tailscaled who it
is (`tailscale whois`). Tailscale's own ACLs/sharing already decided this
device may connect; we only need to know *who* it is.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
import time

from fastapi import HTTPException, Request, WebSocket

from .config import CONFIG

_TS_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_whois_cache: dict[str, tuple[float, dict | None]] = {}
_rejected_logged: dict[str, float] = {}


def _client_ip(headers) -> str | None:
    xff = headers.get("x-forwarded-for") or ""
    ip = xff.split(",")[0].strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ip if any(addr in n for n in _TS_NETS) else None


def _whois(ip: str) -> dict | None:
    now = time.time()
    hit = _whois_cache.get(ip)
    if hit and now - hit[0] < 300:
        return hit[1]
    user = None
    try:
        out = subprocess.run(["tailscale", "whois", "--json", ip], capture_output=True, text=True, timeout=4)
        if out.returncode == 0:
            d = json.loads(out.stdout)
            up = d.get("UserProfile") or {}
            node = d.get("Node") or {}
            login = up.get("LoginName") or ""
            if login and login != "tagged-devices":
                user = {"login": login, "name": up.get("DisplayName") or login, "pic": up.get("ProfilePicURL") or "",
                        "via": "whois", "device": (node.get("ComputedName") or node.get("Name") or "").rstrip(".")}
            elif node.get("Tags"):
                name = (node.get("ComputedName") or node.get("Name") or ip).rstrip(".")
                user = {"login": f"tagged:{name}", "name": f"{name} (tagged device)", "pic": "", "via": "whois"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        user = None
    _whois_cache[ip] = (now, user)
    return user


def _log_reject(headers, why: str) -> None:
    key = (headers.get("x-forwarded-for") or "local") + why
    if time.time() - _rejected_logged.get(key, 0) > 60:  # at most once a minute per client
        _rejected_logged[key] = time.time()
        print(f"auth: rejected request from {headers.get('x-forwarded-for') or 'local (no X-Forwarded-For)'}: {why}", flush=True)


def _in_tailnet(ip: str | None) -> bool:
    try:
        return bool(ip) and any(ipaddress.ip_address(ip) in n for n in _TS_NETS)
    except ValueError:
        return False


def identify(headers, peer: str | None = None) -> dict | None:
    """peer = the TCP peer address. 127.0.0.1 means the request came through
    tailscale serve (trust its headers); a Tailscale IP means a *direct*
    connection on the Tailscale listener: identify by that address only and
    ignore every header (a client could forge them)."""
    mode = CONFIG["auth"]["mode"]
    if mode == "none":
        return {"login": "local", "name": "Local (auth disabled)"}
    if _in_tailnet(peer):
        user = _whois(peer)
        if not user:
            _log_reject({"x-forwarded-for": peer}, "direct connection: tailscale whois could not identify this device")
            return None
        user = {**user, "via": "direct"}
        allowed = CONFIG["auth"]["allowed_users"]
        if allowed and user["login"] not in allowed:
            _log_reject({"x-forwarded-for": peer}, f"{user['login']} is not in auth.allowed_users")
            return None
        return user
    if peer not in (None, "127.0.0.1", "::1"):
        _log_reject({"x-forwarded-for": peer}, "connection from a non-Tailscale address")
        return None
    login = headers.get("tailscale-user-login")
    user = None
    if login:
        user = {"login": login, "name": headers.get("tailscale-user-name") or login,
                "pic": headers.get("tailscale-user-profile-pic") or "", "via": "header"}
    else:
        ip = _client_ip(headers)
        if ip:
            user = _whois(ip)
            if not user:
                _log_reject(headers, "tailscale whois could not identify this device")
        else:
            _log_reject(headers, "no Tailscale identity (not coming through tailscale serve)")
    if not user:
        return None
    allowed = CONFIG["auth"]["allowed_users"]
    if allowed and user["login"] not in allowed:
        _log_reject(headers, f"{user['login']} is not in auth.allowed_users")
        return None
    return user


def require_user(request: Request) -> dict:
    user = identify(request.headers, request.client.host if request.client else None)
    if not user:
        raise HTTPException(401, "Not authorized: open this panel through Tailscale at https://<jetson>.<tailnet>.ts.net "
                                 "from a device logged in to Tailscale (your own tailnet, or one the Jetson is shared with).")
    return user


async def ws_user(ws: WebSocket) -> dict | None:
    user = identify(ws.headers, ws.client.host if ws.client else None)
    if not user:
        await ws.close(code=4401)
    return user
