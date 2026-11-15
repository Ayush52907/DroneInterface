"""Payload servos on the flight controller (ArduPilot), via MAVLink DO_SET_SERVO.

Configuration lives in drone.yaml (`payload:`). Commands go through fclink:
through mavlink-router when it is enabled (works while other programs use
the FC), otherwise by opening the FC port briefly if no other program has it.
"""
from __future__ import annotations

import asyncio
import threading
import time

from fastapi import HTTPException

from . import drone, fclink, hardware

_lock = threading.Lock()
_return_tasks: dict[str, asyncio.Task] = {}
last_result: dict[str, dict] = {}   # config id -> last command result
last_outputs: dict = {}             # servo number -> {"pwm", "t"}


def _configs(d: dict) -> list[dict]:
    """Payload entries with stable ids (their position in drone.yaml)."""
    return [{**c, "id": str(i), "positions": [{**p, "id": f"{i}.{k}"} for k, p in enumerate(c["positions"])]}
            for i, c in enumerate(d["payload"])]


def save(configs: list[dict]) -> dict:
    clean = [{"name": c.get("name"), "servo": c.get("servo"), "return_after": c.get("return_after", 0),
              "positions": [{"name": p.get("name"), "pwm": p.get("pwm"), "kind": p.get("kind")} for p in c.get("positions") or []]}
             for c in configs]
    d = drone.save_section("payload", clean)
    last_result.clear()
    return status_from(d)


def reset() -> dict:
    return save(drone.DEFAULT["payload"])


def port_status(port: str) -> dict:
    import os
    real = os.path.realpath(port)
    if not os.path.exists(real):
        return {"port": port, "present": False, "busy": False, "owners": []}
    owners, _ = hardware.device_owners({real})
    own = owners.get(real, [])
    return {"port": port, "present": True, "busy": bool(own),
            "owners": sorted({(o["unit"] or o["comm"]).replace(".service", "") for o in own})}


def _remember(res: dict) -> None:
    now = time.time()
    for k, v in (res.get("outputs") or {}).items():
        last_outputs[int(k)] = {"pwm": v, "t": now}


async def command(cfg_id: str, pwm: int, user: dict, audit) -> dict:
    d = drone.load()
    cfg = next((c for c in _configs(d) if c["id"] == cfg_id), None)
    if not cfg:
        raise HTTPException(404, "payload configuration not found (drone.yaml changed?)")
    if not 500 <= int(pwm) <= 2500:
        raise HTTPException(400, "PWM must be 500-2500 µs")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Another servo command is in progress")
    try:
        t = _return_tasks.pop(cfg_id, None)
        if t:
            t.cancel()  # a new command replaces a pending auto-return
        try:
            res = await asyncio.to_thread(fclink.set_servo, cfg["servo"], int(pwm))
        except HTTPException as e:
            audit.record(user, "payload.servo", f"servo {cfg['servo']} -> {pwm}", False, str(e.detail))
            raise
    finally:
        _lock.release()
    _remember(res)
    pos = next((p for p in cfg["positions"] if p["pwm"] == int(pwm)), None)
    label = pos["name"] if pos else f"{pwm} µs"
    audit.record(user, "payload.servo", f"{cfg['name']}: {label}", True,
                 f"servo {cfg['servo']} = {pwm} µs, {res.get('ack')} via {res.get('via')}")
    res.update({"t": time.time(), "pwm": int(pwm), "label": label, "user": user.get("login")})
    last_result[cfg_id] = res
    rest = next((p for p in cfg["positions"] if p["kind"] == "rest"), None)
    if cfg.get("return_after") and pos and pos["kind"] == "release" and rest:
        res["returning_in"] = cfg["return_after"]
        _return_tasks[cfg_id] = asyncio.create_task(_auto_return(cfg_id, rest["pwm"], cfg["return_after"], user, audit))
    return res


async def _auto_return(cfg_id: str, pwm: int, delay: float, user: dict, audit) -> None:
    await asyncio.sleep(delay)
    _return_tasks.pop(cfg_id, None)
    for _ in range(3):  # the port may be briefly busy; retry
        try:
            await command(cfg_id, pwm, {**user, "login": user.get("login", "?") + " (auto-return)"}, audit)
            return
        except HTTPException as e:
            if e.status_code != 409:
                return
            await asyncio.sleep(1.5)


async def read_outputs() -> dict:
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "A servo command is in progress")
    try:
        res = await asyncio.to_thread(fclink.read_outputs)
    finally:
        _lock.release()
    _remember(res)
    return res


def status_from(d: dict) -> dict:
    router = d["mavlink_router"]["enabled"]
    if router:
        fclink.start()
        snap = fclink.router_link.tel.snapshot()
        _remember({"outputs": snap.get("servos")} if snap.get("servos") and (snap.get("servos_age") or 99) < 10 else {})
        link = {"mode": "router", "connected": snap.get("connected"), "armed": snap.get("armed"),
                "error": fclink.router_link.error}
    else:
        link = {"mode": "direct", **port_status(d["fc"]["port"])}
    return {"port": d["fc"]["port"], "baud": d["fc"]["baud"], "configs": _configs(d), "link": link,
            "last": last_result, "outputs": {str(k): v for k, v in last_outputs.items()},
            "pending_return": list(_return_tasks.keys())}


def status() -> dict:
    return status_from(drone.load())
