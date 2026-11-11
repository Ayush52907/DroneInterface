"""Live journald streaming and log download."""
from __future__ import annotations

import asyncio
import json
import re
import time

from .services import norm_unit

SINCE_RE = re.compile(r"^[A-Za-z0-9 :\-+.]{1,40}$")


def build_cmd(units: list[str], kernel: bool, priority: int | None, lines: int,
              since: str | None, follow: bool, fmt: str) -> list[str]:
    cmd = ["journalctl", "--no-pager", "-o", fmt, "-n", str(max(0, min(lines, 100000)))]
    if follow:
        cmd.append("-f")
    for u in units:
        cmd += ["-u", norm_unit(u)]
    if kernel:
        cmd.append("-k")
    if priority is not None and 0 <= priority <= 7:
        cmd += ["-p", f"0..{priority}"]
    if since:
        if since == "boot":
            cmd.append("-b")
        elif SINCE_RE.match(since):
            cmd += ["--since", since]
        else:
            raise ValueError("invalid since")
    return cmd


def parse_entry(line: bytes) -> dict | None:
    try:
        e = json.loads(line)
    except ValueError:
        return None
    msg = e.get("MESSAGE", "")
    if isinstance(msg, list):  # binary payload
        msg = bytes(msg).decode(errors="replace")
    elif msg is None:
        msg = ""
    # messages systemd logs *about* a unit (Started/Stopped/Failed) carry UNIT=
    unit = e.get("UNIT") or e.get("_SYSTEMD_UNIT") or ""
    if e.get("_TRANSPORT") == "kernel":
        unit = "kernel"
    try:
        ts = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6
    except ValueError:
        ts = time.time()
    try:
        prio = int(e.get("PRIORITY", 6))
    except ValueError:
        prio = 6
    return {
        "ts": ts,
        "p": prio,
        "unit": unit,
        "id": e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or "",
        "pid": e.get("_PID"),
        "msg": msg,
    }


async def stream(ws, cmd: list[str]) -> None:
    """Run journalctl -f and push batches of entries to the websocket."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 20)
    queue: asyncio.Queue = asyncio.Queue(maxsize=20000)

    async def reader():
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:  # over-long line
                continue
            if not line:
                break
            ent = parse_entry(line)
            if ent:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(ent)
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        await queue.put({"_end": True, "err": err})

    async def watch_client():
        # returns when the browser disconnects
        try:
            while True:
                await ws.receive_text()
        except Exception:
            return

    rtask = asyncio.create_task(reader())
    ctask = asyncio.create_task(watch_client())
    try:
        while not ctask.done():
            batch = []
            try:
                item = await asyncio.wait_for(queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            end = None
            while True:
                if item.get("_end"):
                    end = item
                    break
                batch.append(item)
                if len(batch) >= 500 or queue.empty():
                    break
                item = queue.get_nowait()
            if batch:
                await ws.send_text(json.dumps({"type": "entries", "entries": batch}))
            if end:
                await ws.send_text(json.dumps({"type": "end", "error": end["err"]}))
                break
            await asyncio.sleep(0.1)  # coalesce bursts into fewer frames
    finally:
        for t in (rtask, ctask):
            t.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
