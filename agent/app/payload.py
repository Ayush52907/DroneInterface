"""Payload servos on the flight controller (ArduPilot), via MAVLink DO_SET_SERVO.

No permanent connection and no mavlink-router: for each command the panel
opens the FC port, waits for a heartbeat, sends the command, waits for the
COMMAND_ACK, reads back SERVO_OUTPUT_RAW to show what the FC is outputting,
and closes the port again. If another program has the port open (e.g.
drop_drop.py), the panel refuses instead of corrupting that program's link.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid

from fastapi import HTTPException

from . import hardware
from .config import CONFIG

# The FC speaks MAVLink 2; pymavlink only decodes it when this is set before import.
os.environ.setdefault("MAVLINK20", "1")

PATH = os.path.join(CONFIG["state_dir"], "payload.json")
MAV_CMD_DO_SET_SERVO = 183
MAV_CMD_REQUEST_MESSAGE = 512
MSG_SERVO_OUTPUT_RAW = 36
_lock = threading.Lock()
_return_tasks: dict[str, asyncio.Task] = {}
last_result: dict[str, dict] = {}   # config id -> last command result
last_outputs: dict = {}             # servo number -> {"pwm", "t"}

# Seeded from drop_drop.py: servos 11 and 12, MID 1500 locks both payloads,
# LOW 1100 releases one, HIGH 1900 releases the other.
DEFAULT = {
    "port": "/dev/ttyUSB0",
    "baud": 115200,
    "configs": [
        {"name": "Servo 12 (payloads 1 & 2)", "servo": 12, "return_after": 0, "positions": [
            {"name": "Low (release payload 1)", "pwm": 1100, "kind": "release"},
            {"name": "Mid (both locked)", "pwm": 1500, "kind": "rest"},
            {"name": "High (release payload 2)", "pwm": 1900, "kind": "release"}]},
        {"name": "Servo 11 (payloads 3 & 4)", "servo": 11, "return_after": 0, "positions": [
            {"name": "Low (release payload 3)", "pwm": 1100, "kind": "release"},
            {"name": "Mid (both locked)", "pwm": 1500, "kind": "rest"},
            {"name": "High (release payload 4)", "pwm": 1900, "kind": "release"}]},
    ],
}


# ---------------------------------------------------------------- configuration
def _with_ids(d: dict) -> dict:
    for c in d["configs"]:
        c.setdefault("id", uuid.uuid4().hex[:8])
        for p in c["positions"]:
            p.setdefault("id", uuid.uuid4().hex[:8])
    return d


def load() -> dict:
    try:
        with open(PATH) as f:
            return _with_ids(json.load(f))
    except (OSError, ValueError):
        d = _with_ids(json.loads(json.dumps(DEFAULT)))
        _write(d)
        return d


def _write(d: dict) -> None:
    with open(PATH + ".tmp", "w") as f:
        json.dump(d, f, indent=2)
    os.replace(PATH + ".tmp", PATH)


def save(d: dict) -> dict:
    port = str(d.get("port") or "").strip()
    if not re.fullmatch(r"/dev/[A-Za-z0-9_./-]+", port) or ".." in port:
        raise HTTPException(400, "port must be a device path like /dev/ttyUSB0")
    try:
        baud = int(d.get("baud") or 0)
    except ValueError:
        baud = 0
    if baud not in (9600, 19200, 38400, 57600, 115200, 230400, 460800, 500000, 921600, 1500000):
        raise HTTPException(400, f"unsupported baud rate {d.get('baud')}")
    configs = []
    for c in d.get("configs") or []:
        name = str(c.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "every configuration needs a name")
        try:
            servo = int(c.get("servo"))
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name}: servo number must be a number")
        if not 1 <= servo <= 32:
            raise HTTPException(400, f"{name}: servo number must be 1-32")
        positions = []
        for p in c.get("positions") or []:
            pname = str(p.get("name") or "").strip()
            try:
                pwm = int(p.get("pwm"))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{name}: PWM must be a number")
            if not pname:
                raise HTTPException(400, f"{name}: every position needs a name")
            if not 500 <= pwm <= 2500:
                raise HTTPException(400, f"{name} / {pname}: PWM must be 500-2500 µs")
            positions.append({"id": p.get("id") or uuid.uuid4().hex[:8], "name": pname, "pwm": pwm,
                              "kind": "release" if p.get("kind") == "release" else "rest"})
        if not positions:
            raise HTTPException(400, f"{name}: add at least one position")
        try:
            ret = max(0.0, min(float(c.get("return_after") or 0), 120.0))
        except (TypeError, ValueError):
            ret = 0.0
        if ret and not any(p["kind"] == "rest" for p in positions):
            raise HTTPException(400, f"{name}: 'return to rest' needs a position marked as rest")
        configs.append({"id": c.get("id") or uuid.uuid4().hex[:8], "name": name, "servo": servo,
                        "return_after": ret, "positions": positions})
    out = {"port": port, "baud": baud, "configs": configs}
    _write(out)
    return out


def reset() -> dict:
    try:
        os.unlink(PATH)
    except OSError:
        pass
    return load()


# ---------------------------------------------------------------- port status
def port_status(port: str) -> dict:
    real = os.path.realpath(port)
    if not os.path.exists(real):
        return {"port": port, "present": False, "busy": False, "owners": []}
    owners, _ = hardware.device_owners({real})
    own = owners.get(real, [])
    return {"port": port, "present": True, "busy": bool(own),
            "owners": [(o["unit"] or o["comm"]).replace(".service", "") for o in own]}


# ---------------------------------------------------------------- MAVLink
def _send_blocking(port: str, baud: int, servo: int, pwm: int, read_back: bool = True) -> dict:
    """Open the port, send DO_SET_SERVO (or nothing if pwm is None), read outputs, close."""
    from pymavlink import mavutil

    st = port_status(port)
    if not st["present"]:
        raise HTTPException(503, f"{port} not found: is the USB-TTL to the flight controller connected?")
    if st["busy"]:
        raise HTTPException(409, f"{port} is in use by {', '.join(st['owners'])}. "
                                 "Sending now would corrupt that program's MAVLink link; stop it first.")
    t0 = time.time()
    m = mavutil.mavlink_connection(port, baud=baud, source_system=255, source_component=191, autoreconnect=False)
    try:
        hb = m.wait_heartbeat(timeout=5)
        if hb is None:
            raise HTTPException(504, f"No heartbeat from the flight controller on {port} at {baud} baud")
        res = {"fc": {"system": m.target_system, "autopilot": hb.autopilot, "armed": bool(hb.base_mode & 128)}}
        if pwm is not None:
            m.mav.command_long_send(m.target_system, m.target_component, MAV_CMD_DO_SET_SERVO, 0,
                                    float(servo), float(pwm), 0, 0, 0, 0, 0)
            ack = None
            end = time.time() + 3
            while time.time() < end:
                msg = m.recv_match(type="COMMAND_ACK", blocking=True, timeout=max(0.1, end - time.time()))
                if msg and msg.command == MAV_CMD_DO_SET_SERVO:
                    ack = msg
                    break
            if ack is None:
                raise HTTPException(504, "Flight controller did not acknowledge the servo command (no COMMAND_ACK)")
            names = {0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED", 4: "FAILED", 5: "IN_PROGRESS"}
            res["ack"] = names.get(ack.result, str(ack.result))
            if ack.result != 0:
                raise HTTPException(502, f"Flight controller rejected the command: {res['ack']}")
        if read_back:
            time.sleep(0.15)
            m.mav.command_long_send(m.target_system, m.target_component, MAV_CMD_REQUEST_MESSAGE, 0,
                                    float(MSG_SERVO_OUTPUT_RAW), 0, 0, 0, 0, 0, 0)
            outs = {}
            end = time.time() + 2
            while time.time() < end:
                msg = m.recv_match(type="SERVO_OUTPUT_RAW", blocking=True, timeout=max(0.1, end - time.time()))
                if not msg:
                    break
                base = 1 + 8 * getattr(msg, "port", 0)
                for i in range(1, 17):
                    v = getattr(msg, f"servo{i}_raw", None)
                    if v is not None:
                        outs[base + i - 1] = v  # servoN_raw -> output N (+8 per port); 0 = no signal
                if outs:
                    break
            now = time.time()
            for k, v in outs.items():
                last_outputs[k] = {"pwm": v, "t": now}
            res["outputs"] = {str(k): v for k, v in sorted(outs.items())}
            if pwm is not None:
                res["readback"] = outs.get(servo)
        res["ms"] = round((time.time() - t0) * 1000)
        return res
    finally:
        try:
            m.close()
        except Exception:
            pass


async def command(cfg_id: str, pwm: int, user: dict, audit) -> dict:
    d = load()
    cfg = next((c for c in d["configs"] if c["id"] == cfg_id), None)
    if not cfg:
        raise HTTPException(404, "configuration not found")
    if not 500 <= int(pwm) <= 2500:
        raise HTTPException(400, "PWM must be 500-2500 µs")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Another servo command is in progress")
    try:
        t = _return_tasks.pop(cfg_id, None)
        if t:
            t.cancel()  # a new command replaces a pending auto-return
        try:
            res = await asyncio.to_thread(_send_blocking, d["port"], d["baud"], cfg["servo"], int(pwm))
        except HTTPException as e:
            audit.record(user, "payload.servo", f"servo {cfg['servo']} -> {pwm}", False, str(e.detail))
            raise
    finally:
        _lock.release()
    pos = next((p for p in cfg["positions"] if p["pwm"] == int(pwm)), None)
    label = pos["name"] if pos else f"{pwm} µs"
    audit.record(user, "payload.servo", f"{cfg['name']}: {label}", True, f"servo {cfg['servo']} = {pwm} µs, {res.get('ack')}")
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
    d = load()
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "A servo command is in progress")
    try:
        return await asyncio.to_thread(_send_blocking, d["port"], d["baud"], 0, None)
    finally:
        _lock.release()


def status() -> dict:
    d = load()
    return {**d, "port_status": port_status(d["port"]), "last": last_result,
            "outputs": {str(k): v for k, v in last_outputs.items()},
            "pending_return": list(_return_tasks.keys())}
