"""Bridge to the ROS 2 probe process (ros_helper.py).

The probe starts on first use and exits after IDLE_STOP seconds without
requests, so ROS costs nothing while nobody is looking.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from collections import deque

from .config import CONFIG

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ros_helper.py")
IDLE_STOP = 120
DISCOVERY_WAIT = 3.0


class RosBridge:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.last_used = 0.0
        self.started_at = 0.0
        self.stderr: deque = deque(maxlen=40)
        self.lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    def setup_files(self) -> list[str]:
        return [p for p in CONFIG["ros"]["setup"] if os.path.isfile(p)]

    def status(self) -> dict:
        files = self.setup_files()
        return {
            "available": bool(files),
            "setup": CONFIG["ros"]["setup"],
            "sourced": files,
            "missing": [p for p in CONFIG["ros"]["setup"] if p not in files],
            "running": self.proc is not None and self.proc.returncode is None,
            "domain_id": CONFIG["ros"]["domain_id"],
            "stderr": list(self.stderr)[-10:],
        }

    async def _start(self) -> None:
        files = self.setup_files()
        if not files:
            raise RuntimeError("ROS 2 not found: no setup.bash in ros.setup (config.yaml)")
        src = " && ".join(f"source {shlex.quote(f)}" for f in files)
        env = dict(os.environ)
        if CONFIG["ros"]["domain_id"] is not None:
            env["ROS_DOMAIN_ID"] = str(CONFIG["ros"]["domain_id"])
        self.proc = await asyncio.create_subprocess_exec(
            "bash", "-c", f"{src} && exec /usr/bin/python3 -u {shlex.quote(HELPER)}",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, limit=8 << 20)
        self.started_at = time.monotonic()
        ready = asyncio.get_running_loop().create_future()
        self.pending[0] = ready
        asyncio.create_task(self._read_stdout(self.proc))
        asyncio.create_task(self._read_stderr(self.proc))
        try:
            await asyncio.wait_for(ready, 30)
        except Exception:
            await self.stop()
            raise RuntimeError("ROS 2 helper failed to start: " + " | ".join(list(self.stderr)[-5:]))
        await asyncio.sleep(DISCOVERY_WAIT)  # let DDS discovery find the graph
        if not self._idle_task or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())

    async def _read_stdout(self, proc) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(RuntimeError(msg.get("error")))
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("ROS 2 helper exited: " + " | ".join(list(self.stderr)[-3:])))
        self.pending.clear()

    async def _read_stderr(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            self.stderr.append(line.decode(errors="replace").rstrip())

    async def _idle_watch(self) -> None:
        while self.proc and self.proc.returncode is None:
            await asyncio.sleep(10)
            if time.monotonic() - self.last_used > IDLE_STOP:
                await self.stop()
                return

    def warm_up(self) -> None:
        """Start the probe in the background (no-op if starting/running)."""
        if getattr(self, "_warming", None) and not self._warming.done():
            return
        self._warming = asyncio.create_task(self.call("ping", timeout=60))
        self._warming.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.stdin.close()
                await asyncio.wait_for(p.wait(), 3)
            except Exception:
                p.kill()

    async def call(self, cmd: str, args: dict | None = None, timeout: float = 15.0):
        self.last_used = time.monotonic()
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                await self._start()
        rid = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self.proc.stdin.write((json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)
            self.last_used = time.monotonic()


bridge = RosBridge()
