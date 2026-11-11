"""Alert engine: a few rules evaluated every few seconds on cheap checks.

Rules: camera_lost, usb_ttl (missing / unstable / no serial port),
port_conflict, gimbal (not in flight position). Each problem raises one
alert when it starts and resolves it when it clears; the browser shows
them in the panel and as browser notifications.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import time
import uuid
from collections import deque
from urllib.parse import urlparse

from . import hardware
from .config import CONFIG
from .gimbal import gimbal

PATH = os.path.join(CONFIG["state_dir"], "alerts.json")


class Engine:
    def __init__(self) -> None:
        self.active: dict[str, dict] = {}
        self.history: deque = deque(maxlen=200)
        self.seq = 0
        self.mute_until = 0.0
        self._pending: dict[str, float] = {}  # key -> first time the condition was seen
        self._usb_present: dict[str, bool] = {}
        self._usb_drops: dict[str, deque] = {}
        self._cam_ok: bool | None = None
        self._last_cam_check = 0.0
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            d = json.load(open(PATH))
            self.history.extend(d.get("history", []))
            self.mute_until = d.get("mute_until", 0)
            self.seq = d.get("seq", 0)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"history": list(self.history), "mute_until": self.mute_until, "seq": self.seq}, f)
        os.replace(tmp, PATH)

    # ------------------------------------------------------------ raise / clear
    def _raise(self, key: str, rule: str, level: str, title: str, detail: str, delay: float = 0) -> None:
        if key in self.active:
            a = self.active[key]
            if a["detail"] != detail or a["title"] != title:
                a["detail"], a["title"] = detail, title
                self.seq += 1
            return
        first = self._pending.setdefault(key, time.time())
        if time.time() - first < delay:
            return
        a = {"id": uuid.uuid4().hex[:8], "key": key, "rule": rule, "level": level, "title": title,
             "detail": detail, "since": first, "resolved": None, "acked": False}
        self.active[key] = a
        self.history.appendleft(dict(a))
        self.seq += 1
        self._save()

    def _clear(self, key: str, note: str = "") -> None:
        self._pending.pop(key, None)
        a = self.active.pop(key, None)
        if not a:
            return
        now = time.time()
        for h in self.history:
            if h["id"] == a["id"]:
                h["resolved"] = now
                if note:
                    h["resolved_note"] = note
                break
        self.seq += 1
        self._save()

    # ------------------------------------------------------------ rules
    async def _camera(self) -> None:
        if not CONFIG["alerts"]["camera_lost"]["enabled"] or not CONFIG["cameras"]:
            return
        if time.time() - self._last_cam_check < 10:
            return
        self._last_cam_check = time.time()
        cam = CONFIG["cameras"][0]
        u = urlparse(cam["url"])
        if not u.hostname:
            return
        r = await hardware.tcp(u.hostname, u.port or 554, timeout=2)
        self._cam_ok = r["ok"]
        key = "camera_lost"
        if r["ok"]:
            self._clear(key, "camera answering again")
            return
        nets = await hardware.network()
        eth_down = [n["name"] for n in nets if n["kind"] == "ethernet" and not n["carrier"]]
        detail = (f"Ethernet link down on {', '.join(eth_down)}: check the cable / camera power"
                  if eth_down else f"{u.hostname}:{u.port or 554} not answering ({r['detail']})")
        self._raise(key, "camera_lost", "critical", f"Camera lost: {cam.get('name', 'camera')}", detail, delay=15)

    def _usb(self) -> None:
        cfg = CONFIG["alerts"]["usb_ttl"]
        if not cfg["enabled"]:
            return
        now = time.time()
        for dev in cfg["devices"] or []:
            name = dev.get("name") or f"{dev['vid']}:{dev['pid']}"
            vid, pid = str(dev["vid"]).lower(), str(dev["pid"]).lower()
            found, tty = None, None
            for p in glob.glob("/sys/bus/usb/devices/*"):
                if ":" in os.path.basename(p):
                    continue
                try:
                    if open(p + "/idVendor").read().strip() == vid and open(p + "/idProduct").read().strip() == pid:
                        found = os.path.basename(p)
                        ttys = glob.glob(p + "/*/ttyUSB*") + glob.glob(p + "/*/tty/tty*")
                        tty = "/dev/" + os.path.basename(ttys[0]) if ttys else None
                        break
                except OSError:
                    continue
            key = f"usb:{vid}:{pid}"
            was = self._usb_present.get(key)
            drops = self._usb_drops.setdefault(key, deque(maxlen=20))
            if was and not found:
                drops.append(now)
            self._usb_present[key] = bool(found)
            window = float(cfg["flap_minutes"]) * 60
            recent = [t for t in drops if t > now - window]
            if not found:
                self._clear(key + ":flap")
                self._raise(key, "usb_ttl", "critical", f"{name} disconnected",
                            "USB device not detected: check the cable / connector", delay=5)
            elif found and not tty:
                self._clear(key)
                self._raise(key + ":tty", "usb_ttl", "critical", f"{name}: no serial port",
                            f"Device is on USB port {found} but no /dev/ttyUSB* was created (driver problem?)", delay=10)
            else:
                self._clear(key, f"connected again ({tty})")
                self._clear(key + ":tty")
                if len(recent) >= int(cfg["flap_count"]):
                    self._raise(key + ":flap", "usb_ttl", "warning", f"{name}: unstable connection",
                                f"Disconnected {len(recent)} times in the last {cfg['flap_minutes']} min: loose cable or power issue")
                elif not recent:
                    self._clear(key + ":flap")

    def _conflicts(self) -> None:
        if not CONFIG["alerts"]["port_conflict"]["enabled"]:
            return
        now = hardware.monitor.conflicts()
        for port, who in now.items():
            self._raise(f"conflict:{port}", "port_conflict", "critical", f"Serial port conflict on {port}",
                        f"Open in several programs at once ({who}). They will corrupt each other's data.")
        for key in [k for k in self.active if k.startswith("conflict:")]:
            if key.split(":", 1)[1] not in now:
                self._clear(key, "only one program now")

    async def _gimbal(self) -> None:
        cfg = CONFIG["alerts"]["gimbal"]
        key = "gimbal"
        if not cfg["enabled"] or not CONFIG["gimbal"]["enabled"]:
            return
        if self._cam_ok is False:  # covered by "camera lost"
            self._clear(key)
            return
        if not gimbal.busy:
            await gimbal.refresh()
        pos = gimbal.in_position()
        if pos is None or pos or gimbal.busy:
            if pos:
                self._clear(key, "back in flight position")
            return
        tgt, _ = gimbal.target()
        att = gimbal.att or {}
        mode = {0: "LOCK", 1: "FOLLOW", 2: "FPV"}.get(gimbal.mode, "?")
        why = "manual control active" if gimbal.manual or gimbal.mode == 0 else f"mode {mode}"
        self._raise(key, "gimbal", "warning", "Gimbal not in flight position",
                    f"Pitch {att.get('pitch', 0):.1f}° (target {tgt:g}°), yaw {att.get('yaw', 0):.1f}°, {why}. Press Flight position on the Camera page.",
                    delay=float(cfg["after_seconds"]))

    async def run(self) -> None:
        while True:
            try:
                await self._camera()
                await asyncio.to_thread(self._usb)
                self._conflicts()
                await self._gimbal()
            except Exception as e:
                print("alerts:", e)
            await asyncio.sleep(float(CONFIG["alerts"]["check_seconds"]))

    # ------------------------------------------------------------ API
    def summary(self) -> dict:
        return {"seq": self.seq, "muted": self.mute_until > time.time(),
                "active": sorted(self.active.values(), key=lambda a: (a["level"] != "critical", a["since"]))}

    def full(self) -> dict:
        return {**self.summary(), "mute_until": self.mute_until, "history": list(self.history)}

    def ack(self, aid: str) -> None:
        for a in self.active.values():
            if a["id"] == aid:
                a["acked"] = True
                self.seq += 1

    def mute(self, minutes: float) -> None:
        self.mute_until = time.time() + minutes * 60 if minutes > 0 else 0
        self.seq += 1
        self._save()


engine = Engine()
