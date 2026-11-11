"""SIYI gimbal control (UDP SDK protocol, port 37260).

Cooperates with the siyi-pitch service instead of fighting it:
  * siyi-pitch runs a 20 Hz loop holding the flight position (pitch -90)
    while the gimbal is in FOLLOW mode, and pauses itself in other modes.
  * Manual control here first switches the gimbal to LOCK mode, so
    siyi-pitch pauses and manual moves stick.
  * "Flight position" switches back to FOLLOW mode, so siyi-pitch itself
    returns the gimbal to its target. If siyi-pitch isn't running it is
    restarted, and the result is verified from the gimbal's reported angle.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import struct
import time
from urllib.parse import urlparse

from . import services
from .config import CONFIG

CMD_ZOOM, CMD_SPEED, CMD_CENTER, CMD_INFO, CMD_MODE, CMD_ATT, CMD_ANGLE = 0x05, 0x07, 0x08, 0x0A, 0x0C, 0x0D, 0x0E
MODE_NAMES = {0: "LOCK", 1: "FOLLOW", 2: "FPV"}
MODE_DATA = {"lock": 3, "follow": 4, "fpv": 5}  # PHOTO_VIDEO_HDR command values
DEADMAN = 0.6  # s: a manual move stops unless the browser keeps renewing it
SETTLE = 1.1   # s: after the gimbal confirms LOCK, give siyi-pitch time (it re-reads the mode ~1/s) to pause


def _crc16(data: bytes) -> int:  # CRC-16/XMODEM (poly 0x1021, init 0), as SIYI
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def packet(cmd: int, data: bytes = b"") -> bytes:
    front = b"\x55\x66\x01" + struct.pack("<HH", len(data), 0) + bytes([cmd]) + data
    return front + struct.pack("<H", _crc16(front))


def _gimbal_host() -> str:
    g = CONFIG["gimbal"]
    if g.get("host"):
        return g["host"]
    for c in CONFIG["cameras"]:
        u = urlparse(c["url"])
        if u.hostname:
            return u.hostname
    return "192.168.144.25"


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner: "Gimbal") -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner._on_packet(data)


class Gimbal:
    def __init__(self) -> None:
        self.transport = None
        self.waiters: dict[int, list[asyncio.Future]] = {}
        self.att: dict | None = None
        self.mode: int | None = None
        self.recording: int | None = None
        self.last_seen = 0.0
        self.manual = False
        self._stop_handle: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()
        self.busy = ""
        self._last_lock = 0.0
        self._lock_seen_at: float | None = None

    # ------------------------------------------------------------ transport
    async def _ensure(self) -> None:
        if self.transport is None:
            loop = asyncio.get_running_loop()
            self.transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), family=socket.AF_INET)

    def _on_packet(self, data: bytes) -> None:
        while len(data) >= 10:
            i = data.find(b"\x55\x66")
            if i < 0:
                return
            data = data[i:]
            if len(data) < 10:
                return
            ln = struct.unpack("<H", data[3:5])[0]
            total = 8 + ln + 2
            if len(data) < total:
                return
            pkt, data = data[:total], data[total:]
            if struct.unpack("<H", pkt[-2:])[0] != _crc16(pkt[:-2]):
                continue
            cmd, payload = pkt[7], pkt[8:8 + ln]
            self.last_seen = time.time()
            if cmd == CMD_ATT and len(payload) >= 6:
                y, p, r = struct.unpack("<hhh", payload[:6])
                self.att = {"yaw": y / 10, "pitch": p / 10, "roll": r / 10, "t": time.time()}
            elif cmd == CMD_INFO and len(payload) >= 5:
                self.recording, self.mode = payload[3], payload[4]
            for fut in self.waiters.pop(cmd, []):
                if not fut.done():
                    fut.set_result(payload)

    async def send(self, cmd: int, data: bytes = b"") -> None:
        await self._ensure()
        self.transport.sendto(packet(cmd, data), (_gimbal_host(), int(CONFIG["gimbal"]["port"])))

    async def request(self, cmd: int, data: bytes = b"", timeout: float = 1.0) -> bytes | None:
        fut = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(cmd, []).append(fut)
        await self.send(cmd, data)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------ state
    def target(self) -> tuple[float, str]:
        """Flight pitch: read from the siyi-pitch script, so they never disagree."""
        path = os.path.expanduser(CONFIG["gimbal"]["flight_script"])
        try:
            m = re.search(r"^\s*TARGET_PITCH\s*=\s*(-?\d+(?:\.\d+)?)", open(path).read(), re.M)
            if m:
                return float(m.group(1)), path
        except OSError:
            pass
        return float(CONFIG["gimbal"]["flight_pitch"]), "config.yaml"

    async def refresh(self) -> bool:
        a, i = await asyncio.gather(self.request(CMD_ATT), self.request(CMD_INFO))
        return a is not None

    def in_position(self) -> bool | None:
        if not self.att or time.time() - self.att["t"] > 15:
            return None
        tgt, _ = self.target()
        return (self.mode == 1 and abs(self.att["pitch"] - tgt) <= float(CONFIG["gimbal"]["tolerance"])
                and abs(self.att["yaw"]) <= float(CONFIG["gimbal"]["yaw_tolerance"]))

    async def status(self, refresh: bool = True) -> dict:
        if refresh:
            await self.refresh()
        tgt, src = self.target()
        unit = services.norm_unit(CONFIG["gimbal"]["service"])
        svc = await services.show([unit])
        online = self.att is not None and time.time() - self.att["t"] < 15
        return {
            "online": online, "host": _gimbal_host(),
            "attitude": self.att if online else None,
            "mode": MODE_NAMES.get(self.mode, None) if online else None,
            "recording": bool(self.recording == 1) if online and self.recording is not None else None,
            # SIYI record status: 0 idle, 1 recording, 2 no TF card, 3 TF card error
            "sd_card": (None if not online or self.recording is None else
                        "missing" if self.recording == 2 else "error" if self.recording == 3 else "ok"),
            "target_pitch": tgt, "target_source": src, "tolerance": CONFIG["gimbal"]["tolerance"],
            "yaw_tolerance": CONFIG["gimbal"]["yaw_tolerance"],
            "in_position": self.in_position(), "manual": self.manual, "busy": self.busy,
            "manual_ready": bool(self.manual and self._lock_seen_at is not None and time.monotonic() - self._lock_seen_at >= SETTLE),
            "service": {"name": unit, "active": svc[0]["active"] if svc else "missing",
                        "sub": svc[0]["sub"] if svc else ""},
        }

    # ------------------------------------------------------------ commands
    async def set_mode(self, name: str) -> None:
        await self.send(CMD_MODE, bytes([MODE_DATA[name]]))
        self.manual = name != "follow"
        if name == "follow":
            self._lock_seen_at = None

    async def move(self, yaw: int, pitch: int) -> None:
        """Rotate at a speed (-100..100) for DEADMAN seconds unless renewed.

        Manual control needs the gimbal in LOCK mode *and* siyi-pitch to have
        noticed (it re-reads the mode about once a second). The gimbal itself
        takes 1-3 s to switch, so we wait for its confirmation instead of a
        fixed delay; the browser keeps resending while the button is held."""
        yaw, pitch = max(-100, min(100, int(yaw))), max(-100, min(100, int(pitch)))
        now = time.monotonic()
        if not self.manual:
            await self._enter_manual()
        if not await self.manual_ready():
            return
        await self.send(CMD_SPEED, struct.pack("<bb", yaw, pitch))
        if self._stop_handle:
            self._stop_handle.cancel()
        loop = asyncio.get_running_loop()
        self._stop_handle = loop.call_later(DEADMAN, lambda: asyncio.ensure_future(self.stop_move()))

    async def manual_ready(self) -> bool:
        """True once the gimbal reports LOCK and siyi-pitch has had time to pause."""
        now = time.monotonic()
        if self._lock_seen_at is not None and self.mode == 1 and now - self._lock_seen_at > 3:
            await self._enter_manual()  # gimbal went back to FOLLOW on its own (siyi-pitch restarted?)
            return False
        if self._lock_seen_at is None:
            await self.request(CMD_INFO, timeout=0.3)
            if self.mode == 0:
                self._lock_seen_at = time.monotonic()
            elif now - self._last_lock > 1.5:  # not switched yet: re-send (UDP packet may be lost)
                await self.set_mode("lock")
                self._last_lock = now
            return False
        return now - self._lock_seen_at >= SETTLE

    async def _enter_manual(self) -> None:
        await self.set_mode("lock")  # also sets self.manual = True
        self._last_lock = time.monotonic()
        self._lock_seen_at = None

    async def stop_move(self) -> None:
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        await self.send(CMD_SPEED, struct.pack("<bb", 0, 0))

    async def simple(self, action: str) -> None:
        if action == "center":
            if not self.manual:
                await self._enter_manual()
            end = time.monotonic() + 5
            while not await self.manual_ready() and time.monotonic() < end:
                await asyncio.sleep(0.2)
            await self.send(CMD_CENTER, b"\x01")
        elif action in ("zoom_in", "zoom_out", "zoom_stop"):
            await self.send(CMD_ZOOM, {"zoom_in": b"\x01", "zoom_out": b"\xff", "zoom_stop": b"\x00"}[action])
        elif action == "photo":
            await self.send(CMD_MODE, b"\x00")
        elif action == "record":
            await self.send(CMD_MODE, b"\x02")
        elif action == "lock":
            await self.set_mode("lock")
        else:
            raise ValueError(f"unknown gimbal action {action}")

    async def flight_position(self, log=None) -> dict:
        """Return to the flight position via siyi-pitch. Verified."""
        say = log or (lambda m: None)
        async with self._lock:
            self.busy = "flight"
            try:
                tgt, _ = self.target()
                tol = float(CONFIG["gimbal"]["tolerance"])
                unit = services.norm_unit(CONFIG["gimbal"]["service"])
                await self.stop_move()
                svc = await services.show([unit])
                running = bool(svc) and svc[0]["active"] == "active"
                if not running:
                    say(f"{unit} is not running: starting it")
                    ok, msg = await services.action(unit, "restart")
                    if not ok:
                        return {"ok": False, "detail": f"could not start {unit}: {msg}"}
                # 1) move both axes directly with the gimbal's own (fast) controller
                say(f"Moving gimbal to yaw 0°, pitch {tgt:g}°")
                await self.send(CMD_ANGLE, struct.pack("<hh", 0, int(round(tgt * 10))))
                if not await self._wait_angles(0.0, tgt, tol, 8, say):
                    say("  direct move did not finish; handing over to siyi-pitch anyway")
                # 2) FOLLOW: siyi-pitch takes over and holds the position
                say("Switching gimbal to FOLLOW mode (siyi-pitch holds it)")
                await self.set_mode("follow")
                ok = await self._wait_pitch(tgt, tol, 12, say)
                if not ok and running:
                    say(f"Not at {tgt:g}° yet: restarting {unit}")
                    await services.action(unit, "restart")
                    await asyncio.sleep(1)
                    await self.set_mode("follow")
                    ok = await self._wait_pitch(tgt, tol, 20, say)
                p = self.att["pitch"] if self.att else None
                y = self.att["yaw"] if self.att else None
                if ok:
                    self.manual = False
                    return {"ok": True, "detail": f"In flight position: pitch {p:.1f}° (target {tgt:g}°), yaw {y:.1f}°"}
                return {"ok": False, "detail": "Gimbal did not reach the flight position" + (f" (pitch {p:.1f}°)" if p is not None else " (no response from gimbal)")}
            finally:
                self.busy = ""

    async def _wait_angles(self, yaw_t: float, pitch_t: float, tol: float, secs: float, say) -> bool:
        """Poll attitude only (fast) until both axes are near their targets."""
        end = time.monotonic() + secs
        yaw_tol = min(tol, float(CONFIG["gimbal"]["yaw_tolerance"]))
        last = 0.0
        while time.monotonic() < end:
            await self.request(CMD_ATT, timeout=0.5)
            a = self.att
            if a and abs(a["pitch"] - pitch_t) <= tol / 2 and abs(a["yaw"] - yaw_t) <= yaw_tol / 2:
                return True
            if a and time.monotonic() - last > 1.5:
                say(f"  pitch {a['pitch']:.1f}°, yaw {a['yaw']:.1f}°")
                last = time.monotonic()
            await asyncio.sleep(0.2)
        return False

    async def _wait_pitch(self, tgt: float, tol: float, secs: float, say) -> bool:
        end = time.monotonic() + secs
        last = 0.0
        while time.monotonic() < end:
            await self.refresh()
            if self.att and self.mode == 1 and abs(self.att["pitch"] - tgt) <= tol:
                return True
            if self.att and time.monotonic() - last > 2:
                say(f"  pitch {self.att['pitch']:.1f}°, mode {MODE_NAMES.get(self.mode, '?')}")
                last = time.monotonic()
            await asyncio.sleep(0.5)
        return False


gimbal = Gimbal()
