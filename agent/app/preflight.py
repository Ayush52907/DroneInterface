"""Preflight checklist: user-editable checks, run concurrently."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid

from . import hardware, services
from .config import AGENT_DIR, CONFIG
from .ros import bridge
from .stats import collector, tailscale_status

PATH = os.path.join(CONFIG["state_dir"], "preflight.json")
DEFAULT = os.path.join(AGENT_DIR, "preflight.default.json")

# type -> required params (used for validation and by the UI)
TYPES = {
    "service": ["unit"],
    "device": ["path"],
    "serial_owner": ["path"],
    "usb": ["vid", "pid"],
    "link": ["iface"],
    "ping": ["host"],
    "tcp": ["host", "port"],
    "rtsp": ["host", "port", "path"],
    "ros_node": ["node"],
    "ros_topic": ["topic", "min_hz"],
    "disk": ["path", "min_gb"],
    "temp": ["max_c"],
    "tailscale": [],
    "gimbal": [],
}


def load() -> list[dict]:
    first = not os.path.exists(PATH)
    src = DEFAULT if first else PATH
    try:
        with open(src) as f:
            checks = json.load(f)
    except (OSError, ValueError):
        checks = []
    for c in checks:
        c.setdefault("id", uuid.uuid4().hex[:8])
        c.setdefault("severity", "critical")
        c.setdefault("enabled", True)
    if first and checks:
        try:
            save(checks)  # persist ids so results stay matched to checks
        except Exception:
            pass
    return checks


def save(checks: list[dict]) -> list[dict]:
    clean = []
    for c in checks:
        t = c.get("type")
        if t not in TYPES:
            raise ValueError(f"unknown check type: {t}")
        for p in TYPES[t]:
            if c.get(p) in (None, ""):
                if t == "rtsp" and p == "path":
                    c[p] = "/"
                    continue
                raise ValueError(f"check '{c.get('name') or t}' needs '{p}'")
        c["id"] = c.get("id") or uuid.uuid4().hex[:8]
        c["severity"] = "warning" if c.get("severity") == "warning" else "critical"
        c["name"] = (c.get("name") or "").strip() or f"{t} check"
        if t == "service":
            c["unit"] = services.norm_unit(str(c["unit"]))
        clean.append(c)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clean, f, indent=2)
    os.replace(tmp, PATH)
    return clean


def _res(c, ok, detail):
    return {"id": c["id"], "status": "pass" if ok is True else "fail" if ok is False else "unknown", "detail": detail}


async def _run_check(c: dict, ctx: dict) -> dict:
    t = c["type"]
    try:
        if t == "service":
            s = ctx["services"].get(c["unit"])
            if not s:
                return _res(c, False, "unit not found")
            ok = s["active"] == "active"
            d = f"{s['active']} ({s['sub']})"
            if ok and s.get("restarts"):
                d += f", {s['restarts']} restart(s)"
            return _res(c, ok, d)
        if t == "device":
            ok = os.path.exists(c["path"])
            return _res(c, ok, "present" if ok else "missing")
        if t == "serial_owner":
            if not os.path.exists(c["path"]):
                return _res(c, False, "device missing")
            owners, hidden = ctx["owners"]
            o = owners.get(os.path.realpath(c["path"]), [])
            groups = hardware.owner_groups(o)
            names = [(g[0]["unit"] or g[0]["comm"]).replace(".service", "") for g in groups]
            if len(groups) == 1:
                return _res(c, True, "used by " + names[0])
            if len(groups) > 1:
                return _res(c, False, "CONFLICT: open in " + ", ".join(names) + " at the same time")
            return _res(c, None if hidden else False, "not opened by any program" + (" (root processes not visible)" if hidden else ""))
        if t == "usb":
            want = (str(c["vid"]).lower(), str(c["pid"]).lower())
            hit = [d for d in ctx["usb"] if (d["vid"], d["pid"]) == want]
            return _res(c, bool(hit), f"{hit[0]['product'] or 'device'} on {hit[0]['speed']}" if hit else "not connected")
        if t == "link":
            n = next((i for i in ctx["net"] if i["name"] == c["iface"]), None)
            if not n:
                return _res(c, False, "interface not found")
            ok = n["carrier"]
            d = ("up" + (f", {n['speed']} Mb/s" if n.get("speed") else "") + (f", {', '.join(n['ipv4'])}" if n["ipv4"] else "")) if ok else "no link (cable unplugged or peer off)"
            return _res(c, ok, d)
        if t == "ping":
            r = await hardware.ping(c["host"])
            return _res(c, r["ok"], f"{r['ms']} ms" if r["ok"] else r["detail"])
        if t == "tcp":
            r = await hardware.tcp(c["host"], int(c["port"]))
            return _res(c, r["ok"], r["detail"] + (f" ({r['ms']} ms)" if r.get("ms") else ""))
        if t == "rtsp":
            r = await hardware.rtsp(c["host"], int(c["port"]), c.get("path") or "/")
            return _res(c, r["ok"], r["detail"])
        if t == "disk":
            du = shutil.disk_usage(c["path"])
            free = du.free / 1e9
            return _res(c, free >= float(c["min_gb"]), f"{free:.1f} GB free (min {c['min_gb']} GB)")
        if t == "temp":
            s = collector.latest or {}
            v = s.get("temp_max")
            if v is None:
                return _res(c, None, "no temperature sensor")
            return _res(c, v <= float(c["max_c"]), f"{v} °C (max {c['max_c']} °C)")
        if t == "gimbal":
            from .gimbal import gimbal
            await gimbal.refresh()
            pos = gimbal.in_position()
            tgt, _ = gimbal.target()
            if pos is None:
                return _res(c, False, "gimbal not responding")
            p = gimbal.att["pitch"]
            mode = {0: "LOCK", 1: "FOLLOW", 2: "FPV"}.get(gimbal.mode, "?")
            return _res(c, pos, f"pitch {p:.1f}° (target {tgt:g}°), yaw {gimbal.att['yaw']:.1f}°, {mode}")
        if t == "tailscale":
            ts = await tailscale_status()
            return _res(c, bool(ts and ts.get("online")), "connected" if ts and ts.get("online") else "offline")
        if t == "ros_node":
            g = ctx.get("ros_graph")
            if isinstance(g, Exception):
                return _res(c, None, str(g) if "starting" in str(g) else f"ROS unavailable: {g}")
            want = c["node"] if c["node"].startswith("/") else "/" + c["node"]
            ok = any(n["full"] == want for n in g["nodes"])
            return _res(c, ok, "running" if ok else "not in ROS graph")
        if t == "ros_topic":
            r = ctx.get("ros_rates")
            if isinstance(r, Exception):
                return _res(c, None, f"ROS unavailable: {r}")
            st = (r or {}).get("rates", {}).get(c["topic"])
            if not st:
                return _res(c, False, "topic not found / no publisher")
            min_hz = float(c["min_hz"])
            ok = st["hz"] >= min_hz if min_hz > 0 else st["count"] > 0
            return _res(c, ok, f"{st['hz']} Hz" + (f" (min {min_hz:g})" if min_hz > 0 else ""))
        return _res(c, None, f"unknown type {t}")
    except Exception as e:
        return _res(c, None, f"error: {e}")


async def run(checks: list[dict] | None = None) -> dict:
    checks = [c for c in (checks or load()) if c.get("enabled", True)]
    t0 = time.time()
    types = {c["type"] for c in checks}
    ctx: dict = {}
    units = sorted({c["unit"] for c in checks if c["type"] == "service"})

    async def get_services():
        ctx["services"] = {s["name"]: s for s in await services.show(units)} if units else {}

    async def get_hw():
        if types & {"link"}:
            ctx["net"] = await hardware.network()
        if types & {"usb"}:
            ctx["usb"] = await asyncio.to_thread(hardware.usb_devices)
        if types & {"serial_owner"}:
            paths = {os.path.realpath(c["path"]) for c in checks if c["type"] == "serial_owner"}
            ctx["owners"] = await asyncio.to_thread(hardware.device_owners, paths)

    async def get_ros():
        if not types & {"ros_node", "ros_topic"}:
            return
        warming = getattr(bridge, "_warming", None)
        if not bridge.status()["running"] or (warming is not None and not warming.done()):
            bridge.warm_up()
            err = RuntimeError("starting ROS 2 probe, results in a few seconds")
            ctx["ros_graph"] = ctx["ros_rates"] = err
            return
        try:
            ctx["ros_graph"] = await bridge.call("graph")
        except Exception as e:
            ctx["ros_graph"] = e
            ctx["ros_rates"] = e
            return
        topics = [c["topic"] for c in checks if c["type"] == "ros_topic"]
        if topics:
            try:
                ctx["ros_rates"] = await bridge.call("measure", {"topics": topics, "duration": 2.5}, timeout=20)
            except Exception as e:
                ctx["ros_rates"] = e

    await asyncio.gather(get_services(), get_hw(), get_ros())
    results = await asyncio.gather(*(_run_check(c, ctx) for c in checks))
    by = {c["id"]: c for c in checks}
    crit_fail = [r for r in results if r["status"] != "pass" and by[r["id"]]["severity"] == "critical"]
    warn_fail = [r for r in results if r["status"] != "pass" and by[r["id"]]["severity"] == "warning"]
    verdict = "nogo" if crit_fail else "warn" if warn_fail else "go"
    return {"verdict": verdict, "results": results, "t": time.time(), "ms": round((time.time() - t0) * 1000),
            "counts": {"pass": sum(r["status"] == "pass" for r in results), "total": len(results),
                       "critical_fail": len(crit_fail), "warning_fail": len(warn_fail)}}
