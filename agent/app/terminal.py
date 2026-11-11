"""Web terminal: PTY sessions that survive browser disconnects.

On the Jetson the shell is started via `sudo dpctl shell`, which runs
`systemd-run --pty --uid=<panel user> bash -l`: the shell lives in its own
transient unit (not throttled by the panel's CPU/RAM caps) and is never root.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import termios
import time
import uuid

from .config import CONFIG
from .services import DPCTL

MAX_SESSIONS = 8
BUFFER = 256 * 1024


class Session:
    def __init__(self, user: dict, cols: int, rows: int) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.user = user.get("login", "?")
        self.created = time.time()
        self.last_detach = time.time()
        self.buffer = bytearray()
        self.clients: set[asyncio.Queue] = set()
        self.alive = True
        self.master, slave = pty.openpty()
        self.resize(cols, rows, fd=slave)
        if os.path.exists(DPCTL):
            cmd = ["sudo", "-n", DPCTL, "shell"]
        else:
            cmd = [os.environ.get("SHELL", "/bin/bash"), "-l"]
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        def child_setup():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env,
                                     start_new_session=True, preexec_fn=child_setup,
                                     cwd=os.path.expanduser("~"), close_fds=True)
        os.close(slave)
        os.set_blocking(self.master, False)
        asyncio.get_running_loop().add_reader(self.master, self._on_read)

    def resize(self, cols: int, rows: int, fd: int | None = None) -> None:
        cols, rows = max(10, min(int(cols), 500)), max(4, min(int(rows), 300))
        try:
            fcntl.ioctl(self.master if fd is None else fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _on_read(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._ended()
            return
        self.buffer += data
        if len(self.buffer) > BUFFER:
            del self.buffer[: len(self.buffer) - BUFFER]
        for q in list(self.clients):
            q.put_nowait(data)

    def _ended(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            asyncio.get_running_loop().remove_reader(self.master)
        except Exception:
            pass
        for q in list(self.clients):
            q.put_nowait(None)

    def write(self, data: str) -> None:
        if self.alive:
            try:
                os.write(self.master, data.encode())
            except OSError:
                pass

    def close(self) -> None:
        self._ended()
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.proc.poll() is None:
            for sig in (signal.SIGHUP, signal.SIGTERM):
                try:
                    self.proc.send_signal(sig)
                except OSError:
                    pass
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def info(self) -> dict:
        return {"id": self.id, "user": self.user, "created": self.created, "alive": self.alive,
                "attached": len(self.clients), "idle_since": None if self.clients else self.last_detach}


sessions: dict[str, Session] = {}


def create(user: dict, cols: int, rows: int) -> Session:
    alive = [s for s in sessions.values() if s.alive]
    if len(alive) >= MAX_SESSIONS:
        raise RuntimeError(f"Too many terminals open ({MAX_SESSIONS}). Close one first.")
    s = Session(user, cols, rows)
    sessions[s.id] = s
    return s


def kill(sid: str) -> bool:
    s = sessions.pop(sid, None)
    if s:
        s.close()
    return bool(s)


async def reaper() -> None:
    keep = CONFIG["terminal"]["keep_alive_minutes"] * 60
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for sid, s in list(sessions.items()):
            if (not s.alive and not s.clients) or (not s.clients and now - s.last_detach > keep):
                kill(sid)


async def attach(ws, s: Session, cols: int, rows: int) -> None:
    q: asyncio.Queue = asyncio.Queue()
    s.clients.add(q)
    s.resize(cols, rows)
    await ws.send_text(json.dumps({"t": "hello", "id": s.id, "alive": s.alive}))
    if s.buffer:
        await ws.send_bytes(bytes(s.buffer))
    if not s.alive:
        q.put_nowait(None)

    async def pump():
        while True:
            data = await q.get()
            if data is None:
                await ws.send_text(json.dumps({"t": "exit"}))
                return
            # coalesce queued chunks into one frame
            while not q.empty():
                nxt = q.get_nowait()
                if nxt is None:
                    q.put_nowait(None)
                    break
                data += nxt
            await ws.send_bytes(data)

    ptask = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_text()
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            if m.get("t") == "in":
                s.write(m.get("d", ""))
            elif m.get("t") == "resize":
                s.resize(m.get("cols", 80), m.get("rows", 24))
    except Exception:
        pass
    finally:
        ptask.cancel()
        s.clients.discard(q)
        s.last_detach = time.time()
