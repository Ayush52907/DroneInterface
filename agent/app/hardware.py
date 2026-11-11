"""Hardware & connection status, read from sysfs/procfs.

Also runs a tiny background monitor (every 3 s: a few directory listings) that
records plug/unplug and link up/down events, so a cable that dropped out
earlier is still visible later.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import socket
import time
from collections import deque

import psutil

from .config import CONFIG


def _r(path: str) -> str | None:
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


async def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


# ---------------------------------------------------------------- network
def _iface_kind(name: str, path: str) -> str:
    if name == "lo" or name.startswith("tailscale"):
        return "virtual"
    if os.path.exists(os.path.join(path, "wireless")) or os.path.exists(os.path.join(path, "phy80211")):
        return "wifi"
    t = _r(os.path.join(path, "type"))
    if t == "280":
        return "can"
    if not os.path.exists(os.path.join(path, "device")):
        return "virtual"
    drv = os.path.basename(os.path.realpath(os.path.join(path, "device", "driver")))
    if "gadget" in drv:
        return "usb-gadget"
    return "ethernet"


async def _iw(name: str) -> dict:
    if not shutil.which("iw"):
        return {}
    (rc1, info), (rc2, link) = await asyncio.gather(_run(["iw", "dev", name, "info"], 3), _run(["iw", "dev", name, "link"], 3))
    out: dict = {}
    if rc1 == 0:
        for key, rx in (("mode", r"type (\S+)"), ("ssid", r"ssid (.+)"), ("channel", r"channel (\d+) \((\d+) MHz\)(?:, width: (\d+) MHz)?"),
                        ("txpower", r"txpower ([\d.]+) dBm")):
            m = re.search(rx, info)
            if m:
                if key == "channel":
                    out["channel"] = int(m.group(1))
                    out["freq"] = int(m.group(2))
                    out["width"] = int(m.group(3)) if m.group(3) else None
                elif key == "txpower":
                    out["txpower"] = float(m.group(1))
                else:
                    out[key] = m.group(1).strip()
    if rc2 == 0 and "Connected" in link:
        for key, rx in (("signal", r"signal: (-?\d+) dBm"), ("rx_bitrate", r"rx bitrate: ([\d.]+ \S+)"),
                        ("tx_bitrate", r"tx bitrate: ([\d.]+ \S+)"), ("bssid", r"Connected to (\S+)")):
            m = re.search(rx, link)
            if m:
                out[key] = int(m.group(1)) if key == "signal" else m.group(1)
    return out


async def network() -> list[dict]:
    addrs = psutil.net_if_addrs()
    res = []
    wifi_jobs = {}
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        kind = _iface_kind(name, path)
        if kind == "virtual" and name == "lo":
            continue
        st = lambda k: _r(os.path.join(path, "statistics", k))
        speed = _r(os.path.join(path, "speed"))
        try:
            speed = int(speed) if speed and int(speed) > 0 else None
        except ValueError:
            speed = None
        drv = os.path.basename(os.path.realpath(os.path.join(path, "device", "driver"))) if os.path.exists(os.path.join(path, "device")) else None
        item = {
            "name": name,
            "kind": kind,
            "state": _r(os.path.join(path, "operstate")),
            "carrier": _r(os.path.join(path, "carrier")) == "1",
            "admin_up": bool(int(_r(os.path.join(path, "flags")) or "0", 16) & 1),
            "speed": speed,
            "duplex": _r(os.path.join(path, "duplex")),
            "mac": _r(os.path.join(path, "address")),
            "mtu": int(_r(os.path.join(path, "mtu")) or 0),
            "driver": drv,
            "ipv4": [a.address + "/" + str(sum(bin(int(x)).count("1") for x in a.netmask.split(".")))
                     for a in addrs.get(name, []) if a.family == socket.AF_INET and a.netmask],
            "rx_errors": int(st("rx_errors") or 0) + int(st("rx_crc_errors") or 0),
            "tx_errors": int(st("tx_errors") or 0),
            "rx_dropped": int(st("rx_dropped") or 0),
            "rx_bytes": int(st("rx_bytes") or 0),
            "tx_bytes": int(st("tx_bytes") or 0),
        }
        if kind == "wifi":
            wifi_jobs[name] = _iw(name)
        res.append(item)
    if wifi_jobs:
        results = await asyncio.gather(*wifi_jobs.values())
        for (name, _), w in zip(wifi_jobs.items(), results):
            next(i for i in res if i["name"] == name)["wifi"] = w
    order = {"ethernet": 0, "wifi": 1, "can": 2, "usb-gadget": 3, "virtual": 4}
    res.sort(key=lambda i: (order.get(i["kind"], 9), i["name"]))
    return res


async def can_info(name: str) -> dict:
    rc, out = await _run(["ip", "-j", "-d", "-s", "link", "show", name], 3)
    try:
        d = json.loads(out)[0]
    except (ValueError, IndexError):
        return {}
    info = (d.get("linkinfo") or {}).get("info_data") or {}
    bt = info.get("bittiming") or {}
    berr = info.get("berr_counter") or {}
    stats = d.get("stats64") or d.get("stats") or {}
    return {
        "state": info.get("state"),
        "bitrate": bt.get("bitrate"),
        "tx_err": berr.get("tx"), "rx_err": berr.get("rx"),
        "rx_packets": (stats.get("rx") or {}).get("packets"),
        "tx_packets": (stats.get("tx") or {}).get("packets"),
        "restarts": (info.get("restart_ms"), (d.get("linkinfo") or {}).get("info_xstats", {}).get("restarts")),
    }


# ---------------------------------------------------------------- USB
SPEEDS = {"1.5": "USB 1.0 (1.5M)", "12": "USB 1.1 (12M)", "480": "USB 2.0 (480M)", "5000": "USB 3.0 (5G)",
          "10000": "USB 3.1 (10G)", "20000": "USB 3.2 (20G)"}
CLASSES = {"01": "Audio", "02": "Comms", "03": "HID", "06": "Imaging", "07": "Printer", "08": "Storage", "09": "Hub",
           "0a": "CDC data", "0e": "Video", "e0": "Wireless", "ef": "Misc", "fe": "App", "ff": "Vendor"}


def usb_devices() -> list[dict]:
    devs = []
    for path in glob.glob("/sys/bus/usb/devices/*"):
        name = os.path.basename(path)
        if ":" in name:  # interface, not device
            continue
        vid = _r(os.path.join(path, "idVendor"))
        if not vid:
            continue
        pid = _r(os.path.join(path, "idProduct"))
        drivers, classes = set(), set()
        for intf in glob.glob(path + "/" + name.replace("usb", "") + ":*") + glob.glob(path + "/*:*"):
            d = os.path.join(intf, "driver")
            if os.path.exists(d):
                drivers.add(os.path.basename(os.path.realpath(d)))
            c = _r(os.path.join(intf, "bInterfaceClass"))
            if c:
                classes.add(CLASSES.get(c.lower(), c))
        speed = _r(os.path.join(path, "speed")) or ""
        parent = None
        if not name.startswith("usb"):
            parent = name.rsplit(".", 1)[0] if "." in name else "usb" + name.split("-")[0]
        devs.append({
            "id": name, "parent": parent, "vid": vid, "pid": pid,
            "manufacturer": _r(os.path.join(path, "manufacturer")),
            "product": _r(os.path.join(path, "product")),
            "serial": _r(os.path.join(path, "serial")) if not name.startswith("usb") else None,
            "speed": SPEEDS.get(speed, speed + "M" if speed else ""),
            "speed_mbps": float(speed) if speed else None,
            "max_power": _r(os.path.join(path, "bMaxPower")),
            "drivers": sorted(drivers - {"hub"}) or (["hub"] if "hub" in drivers else []),
            "classes": sorted(classes),
            "root_hub": name.startswith("usb"),
        })
    devs.sort(key=lambda d: [int(x) if x.isdigit() else x for x in re.split(r"[-.]", d["id"].replace("usb", "0-"))])
    return devs


# ---------------------------------------------------------------- serial
SERIAL_GLOBS = ["/dev/ttyTHS*", "/dev/ttyTCU*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*"]


def _unit_of(pid: int) -> str | None:
    cg = _r(f"/proc/{pid}/cgroup") or ""
    m = re.findall(r"/([^/]+\.service)", cg)
    return m[-1] if m else None


DPCTL = "/opt/drone-panel/bin/dpctl"


def _ppid(pid: int) -> int:
    st = _r(f"/proc/{pid}/stat") or ""
    try:
        return int(st.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return 0


def _scan_fds() -> tuple[list[tuple[int, str]], bool]:
    """(pid, device) pairs for serial/video devices held open by any process.
    Uses the privileged read-only `dpctl owners` when installed (sees root
    processes too), otherwise only processes we may inspect."""
    import subprocess
    if os.path.exists(DPCTL):
        try:
            out = subprocess.run(["sudo", "-n", DPCTL, "owners"], capture_output=True, text=True, timeout=5).stdout
            pairs = []
            for line in out.splitlines():
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[0].startswith("/proc/"):
                    pairs.append((int(parts[0].split("/")[2]), parts[1].strip()))
            return pairs, False
        except Exception:
            pass
    pairs, hidden = [], False
    for pid_s in os.listdir("/proc"):
        if not pid_s.isdigit():
            continue
        fd_dir = f"/proc/{pid_s}/fd"
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            hidden = True
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                pairs.append((int(pid_s), os.readlink(os.path.join(fd_dir, fd))))
            except OSError:
                continue
    return pairs, hidden


def device_owners(paths: set[str]) -> tuple[dict[str, list[dict]], bool]:
    """Which processes hold these device files open."""
    pairs, hidden = _scan_fds()
    owners: dict[str, list[dict]] = {p: [] for p in paths}
    for pid, target in pairs:
        if target not in owners or any(o["pid"] == pid for o in owners[target]):
            continue
        comm = _r(f"/proc/{pid}/comm") or "?"
        cmd = (_r(f"/proc/{pid}/cmdline") or "").replace("\0", " ").strip()
        owners[target].append({"pid": pid, "ppid": _ppid(pid), "comm": comm, "cmd": cmd[:200], "unit": _unit_of(pid)})
    return owners, hidden


def owner_groups(owners: list[dict]) -> list[list[dict]]:
    """Group processes that share a device because one inherited it from the
    other (parent/child), or belong to the same service. More than one group
    means independent programs are fighting over the same port."""
    groups: list[list[dict]] = []
    pids = {o["pid"] for o in owners}

    def root_of(o):
        p = o["ppid"]
        seen = 0
        while p in pids and seen < 50:
            o = next(x for x in owners if x["pid"] == p)
            p = o["ppid"]
            seen += 1
        return o["unit"] or f"pid:{o['pid']}"

    by: dict[str, list[dict]] = {}
    for o in owners:
        key = o["unit"] or root_of(o)
        by.setdefault(key, []).append(o)
    groups = list(by.values())
    return groups


def serial_ports() -> list[dict]:
    devs = sorted({p for g in SERIAL_GLOBS for p in glob.glob(g)})
    by_id = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        by_id[os.path.realpath(link)] = os.path.basename(link)
    owners, hidden = device_owners(set(devs))
    out = []
    for d in devs:
        name = os.path.basename(d)
        sys_dev = os.path.realpath(f"/sys/class/tty/{name}/device")
        usb = None
        p = sys_dev
        while p and p != "/" and "/usb" in p:
            if os.path.exists(os.path.join(p, "idVendor")):
                usb = {"id": os.path.basename(p), "vid": _r(os.path.join(p, "idVendor")), "pid": _r(os.path.join(p, "idProduct")),
                       "product": _r(os.path.join(p, "product")), "manufacturer": _r(os.path.join(p, "manufacturer"))}
                break
            p = os.path.dirname(p)
        try:
            st = os.stat(d)
            access = os.access(d, os.R_OK | os.W_OK)
        except OSError:
            access = False
        out.append({
            "path": d, "by_id": by_id.get(d), "usb": usb,
            "kind": "Jetson UART" if name.startswith("ttyTHS") else "USB serial" if usb else "UART",
            "owners": owners.get(d, []), "owners_hidden": hidden, "access": access,
            "conflict": len(owner_groups(owners.get(d, []))) > 1,
        })
    return out


# ---------------------------------------------------------------- cameras / i2c
def video_devices() -> list[dict]:
    res = []
    for p in sorted(glob.glob("/sys/class/video4linux/video*")):
        n = os.path.basename(p)
        res.append({"path": "/dev/" + n, "name": _r(os.path.join(p, "name")), "index": _r(os.path.join(p, "index"))})
    paths = {r["path"] for r in res}
    owners, _ = device_owners(paths) if paths else ({}, False)
    for r in res:
        r["owners"] = owners.get(r["path"], [])
    return res


def i2c_buses() -> list[dict]:
    res = []
    for p in sorted(glob.glob("/sys/class/i2c-dev/i2c-*"), key=lambda x: int(x.rsplit("-", 1)[1])):
        n = os.path.basename(p)
        res.append({"bus": int(n.split("-")[1]), "name": _r(os.path.join(p, "name")) or "", "dev": "/dev/" + n})
    return res


async def i2c_scan(bus: int) -> dict:
    if not shutil.which("i2cdetect"):
        return {"error": "i2cdetect not installed (sudo apt install i2c-tools)"}
    rc, out = await _run(["i2cdetect", "-y", "-r", str(int(bus))], 15)
    if rc != 0:
        return {"error": out.strip()}
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        base = int(parts[0].rstrip(":"), 16)
        for i, v in enumerate(parts[1:]):
            if v not in ("--", "UU") and re.fullmatch(r"[0-9a-f]{2}", v):
                found.append({"addr": hex(int(v, 16)), "in_use": False})
            elif v == "UU":
                found.append({"addr": hex(base + i), "in_use": True})
    return {"bus": bus, "devices": found, "raw": out}


# ---------------------------------------------------------------- reachability
async def ping(host: str, timeout: float = 1.0) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", host):
        return {"ok": False, "detail": "invalid host"}
    t0 = time.monotonic()
    rc, out = await _run(["ping", "-c", "1", "-W", str(max(1, int(timeout))), host], timeout + 2)
    m = re.search(r"time[=<]([\d.]+) ms", out)
    if rc == 0:
        return {"ok": True, "ms": float(m.group(1)) if m else round((time.monotonic() - t0) * 1000, 1), "detail": "reachable"}
    return {"ok": False, "detail": "no reply" if rc == 1 else out.strip().splitlines()[-1][:120] if out.strip() else "ping failed"}


async def tcp(host: str, port: int, timeout: float = 1.5) -> dict:
    t0 = time.monotonic()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
        w.close()
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1), "detail": f"port {port} open"}
    except asyncio.TimeoutError:
        return {"ok": False, "detail": f"port {port}: timeout"}
    except OSError as e:
        return {"ok": False, "detail": f"port {port}: {e.strerror or e}"}


async def rtsp(host: str, port: int = 554, path: str = "/", timeout: float = 2.0) -> dict:
    t0 = time.monotonic()
    url = f"rtsp://{host}:{port}{path or '/'}"
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
        w.write(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: drone-panel\r\n\r\n".encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout)
        w.close()
        status = line.decode(errors="replace").strip()
        ok = status.startswith("RTSP/1.0 200")
        return {"ok": ok, "ms": round((time.monotonic() - t0) * 1000, 1),
                "detail": "RTSP stream server answering" if ok else f"unexpected reply: {status[:60]}"}
    except asyncio.TimeoutError:
        return {"ok": False, "detail": "RTSP: timeout"}
    except OSError as e:
        return {"ok": False, "detail": f"RTSP: {e.strerror or e}"}


async def check_target(t: dict) -> dict:
    kind = t.get("kind", "ping")
    host = str(t["host"])
    if kind == "rtsp":
        res = await rtsp(host, int(t.get("port", 554)), t.get("path", "/"))
    elif kind == "tcp":
        res = await tcp(host, int(t.get("port", 80)))
    else:
        res = await ping(host)
    return {**t, **res}


async def targets() -> list[dict]:
    return list(await asyncio.gather(*(check_target(t) for t in CONFIG["network_targets"])))


async def snapshot() -> dict:
    nets, tg = await asyncio.gather(network(), targets())
    for n in nets:
        if n["kind"] == "can":
            n["can"] = await can_info(n["name"])
    serial, usb, video, i2c = await asyncio.to_thread(
        lambda: (serial_ports(), usb_devices(), video_devices(), i2c_buses()))
    return {"network": nets, "usb": usb, "serial": serial, "video": video, "i2c": i2c,
            "targets": tg, "events": list(monitor.events)[::-1][:100], "t": time.time()}


# ---------------------------------------------------------------- monitor
class Monitor:
    def __init__(self) -> None:
        self.events: deque = deque(maxlen=300)
        self._prev: dict | None = None
        self._tick = 0
        self._conflicts: dict[str, str] = {}

    def _state(self) -> dict:
        usb = {}
        for path in glob.glob("/sys/bus/usb/devices/*"):
            n = os.path.basename(path)
            if ":" in n or n.startswith("usb"):
                continue
            label = _r(os.path.join(path, "product")) or f"{_r(os.path.join(path, 'idVendor'))}:{_r(os.path.join(path, 'idProduct'))}"
            usb[n] = label
        links = {}
        for path in glob.glob("/sys/class/net/*"):
            n = os.path.basename(path)
            if _iface_kind(n, path) in ("ethernet", "wifi", "can"):
                links[n] = _r(os.path.join(path, "carrier")) == "1"  # unreadable when admin-down -> False
        devs = {p for g in SERIAL_GLOBS + ["/dev/video*"] for p in glob.glob(g)}
        return {"usb": usb, "links": links, "devs": devs}

    def _add(self, kind: str, msg: str, level: str) -> None:
        self.events.append({"t": time.time(), "kind": kind, "msg": msg, "level": level})

    def poll(self) -> None:
        cur = self._state()
        p = self._prev
        if p is not None:
            for k in cur["usb"].keys() - p["usb"].keys():
                self._add("usb", f"USB connected: {cur['usb'][k]} (port {k})", "ok")
            for k in p["usb"].keys() - cur["usb"].keys():
                self._add("usb", f"USB disconnected: {p['usb'][k]} (port {k})", "bad")
            for k, up in cur["links"].items():
                if k in p["links"] and p["links"][k] != up:
                    self._add("link", f"{k} link {'UP' if up else 'DOWN'}", "ok" if up else "bad")
            for d in sorted(cur["devs"] - p["devs"]):
                self._add("device", f"Device appeared: {d}", "ok")
            for d in sorted(p["devs"] - cur["devs"]):
                self._add("device", f"Device removed: {d}", "bad")
        else:
            self._add("info", "Panel started: monitoring USB, network links and serial/video devices", "")
        self._prev = cur
        self._tick += 1
        if self._tick % 5 == 1:
            self._check_conflicts()

    def _check_conflicts(self) -> None:
        ports = {p for g in SERIAL_GLOBS for p in glob.glob(g)}
        if not ports:
            return
        owners, _ = device_owners(ports)
        now: dict[str, str] = {}
        for port, own in owners.items():
            groups = owner_groups(own)
            if len(groups) > 1:
                now[port] = " + ".join(sorted((g[0]["unit"] or g[0]["comm"]).replace(".service", "") for g in groups))
        for port, who in now.items():
            if self._conflicts.get(port) != who:
                self._add("conflict", f"CONFLICT: {port} is open in several programs at once ({who}). They will steal each other's data", "bad")
        for port in self._conflicts.keys() - now.keys():
            self._add("conflict", f"{port}: conflict resolved, one program now", "ok")
        self._conflicts = now

    def conflicts(self) -> dict[str, str]:
        return dict(self._conflicts)

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll)
            except Exception as e:
                print("hardware monitor:", e)
            await asyncio.sleep(3)


monitor = Monitor()
