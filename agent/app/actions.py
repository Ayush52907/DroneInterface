"""Quick actions: user-defined buttons made of ordered steps.

Step types:
  service  {unit, op: start|stop|restart, wait: true}   wait until active/stopped
  gimbal   {}                                           flight position (via siyi-pitch)
  command  {cmd, timeout}                               bash -lc, as the panel user
  wait     {seconds}
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from . import services
from .config import AGENT_DIR, CONFIG
from .gimbal import gimbal

PATH = os.path.join(CONFIG["state_dir"], "actions.json")
DEFAULT = os.path.join(AGENT_DIR, "actions.default.json")
STEP_TYPES = {"service", "gimbal", "command", "wait"}
_running: dict[str, float] = {}
last_runs: dict[str, dict] = {}


def load() -> list[dict]:
    first = not os.path.exists(PATH)
    try:
        acts = json.load(open(DEFAULT if first else PATH))
    except (OSError, ValueError):
        acts = []
    for a in acts:
        a.setdefault("id", uuid.uuid4().hex[:8])
    if first and acts:
        save(acts)
    for a in acts:
        a["running"] = a["id"] in _running
        a["last"] = last_runs.get(a["id"])
    return acts


def save(acts: list[dict]) -> list[dict]:
    clean = []
    for a in acts:
        name = (a.get("name") or "").strip()
        if not name:
            raise ValueError("every action needs a name")
        steps = []
        for st in a.get("steps") or []:
            t = st.get("type")
            if t not in STEP_TYPES:
                raise ValueError(f"{name}: unknown step type {t}")
            if t == "service":
                if not str(st.get("unit", "")).strip():
                    raise ValueError(f"{name}: service step needs a service name")
                st["unit"] = services.norm_unit(str(st.get("unit", "")))
                st["op"] = st.get("op") or "start"  # the editor shows "start" first
                if st["op"] not in ("start", "stop", "restart"):
                    raise ValueError(f"{name}: service step needs op start/stop/restart")
            if t == "command" and not str(st.get("cmd", "")).strip():
                raise ValueError(f"{name}: command step is empty")
            if t == "wait":
                st["seconds"] = max(0.0, min(float(st.get("seconds", 1)), 600))
            steps.append({k: v for k, v in st.items() if k in ("type", "unit", "op", "wait", "cmd", "timeout", "seconds")})
        if not steps:
            raise ValueError(f"{name}: add at least one step")
        clean.append({"id": a.get("id") or uuid.uuid4().hex[:8], "name": name, "description": (a.get("description") or "").strip(),
                      "color": a.get("color") or "default", "confirm": bool(a.get("confirm")), "steps": steps})
    with open(PATH + ".tmp", "w") as f:
        json.dump(clean, f, indent=2)
    os.replace(PATH + ".tmp", PATH)
    return clean


async def _wait_state(unit: str, want_active: bool, timeout: float = 30) -> tuple[bool, str]:
    end = time.monotonic() + timeout
    state = "?"
    while time.monotonic() < end:
        info = await services.show([unit])
        state = info[0]["active"] if info else "missing"
        if want_active and state == "active":
            await asyncio.sleep(1.5)  # must still be running shortly after start
            info = await services.show([unit])
            state = info[0]["active"] if info else "missing"
            return state == "active", state
        if want_active and state == "failed":
            return False, state
        if not want_active and state in ("inactive", "failed"):
            return True, state
        await asyncio.sleep(0.5)
    return False, state


async def run(aid: str):
    """Async generator of log lines; last line starts with DONE or FAILED."""
    acts = {a["id"]: a for a in load()}
    a = acts.get(aid)
    if not a:
        yield "FAILED: action not found\n"
        return
    if aid in _running:
        yield "FAILED: this action is already running\n"
        return
    _running[aid] = time.time()
    t0 = time.time()
    ok = True
    try:
        for i, st in enumerate(a["steps"], 1):
            t = st["type"]
            if t == "service":
                label = f"{st['op']} {st['unit']}"
                yield f"[{i}/{len(a['steps'])}] {label}\n"
                good, msg = await services.action(st["unit"], st["op"])
                if not good:
                    yield f"    ✗ {msg}\n"
                    ok = False
                    break
                if st.get("wait", True):
                    good, state = await _wait_state(st["unit"], st["op"] != "stop")
                    yield f"    {'✓' if good else '✗'} {state}\n"
                    if not good:
                        ok = False
                        yield f"    hint: open Services → {st['unit']} for its logs\n"
                        break
                else:
                    yield "    ✓ sent\n"
            elif t == "gimbal":
                yield f"[{i}/{len(a['steps'])}] gimbal → flight position\n"
                lines: list[str] = []
                r = await gimbal.flight_position(lines.append)
                for ln in lines:
                    yield f"    {ln}\n"
                yield f"    {'✓' if r['ok'] else '✗'} {r['detail']}\n"
                if not r["ok"]:
                    ok = False
                    break
            elif t == "wait":
                yield f"[{i}/{len(a['steps'])}] wait {st['seconds']:g}s\n"
                await asyncio.sleep(st["seconds"])
            elif t == "command":
                yield f"[{i}/{len(a['steps'])}] $ {st['cmd']}\n"
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-lc", st["cmd"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    cwd=os.path.expanduser("~"))
                timeout = float(st.get("timeout") or 300)
                end = time.monotonic() + timeout
                try:
                    while True:
                        line = await asyncio.wait_for(proc.stdout.readline(), max(0.1, end - time.monotonic()))
                        if not line:
                            break
                        yield "    " + line.decode(errors="replace")
                    rc = await proc.wait()
                except asyncio.TimeoutError:
                    proc.kill()
                    rc = -1
                    yield f"    ✗ timed out after {timeout:g}s\n"
                if rc != 0:
                    yield f"    ✗ exit code {rc}\n"
                    ok = False
                    break
                yield "    ✓ ok\n"
        dur = time.time() - t0
        yield (f"DONE in {dur:.1f}s\n" if ok else f"FAILED after {dur:.1f}s\n")
    finally:
        _running.pop(aid, None)
        last_runs[aid] = {"t": time.time(), "ok": ok}
