"""System statistics collector.

Reads /proc and /sys directly (no jtop dependency). Samples every
`stats.interval` seconds while someone has the dashboard open, and every
5 s otherwise so the sparkline history is never empty.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import time
from collections import deque

import psutil

from .config import CONFIG

IDLE_INTERVAL = 5.0


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_num(path: str) -> float | None:
    v = _read(path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Hardware discovery (done once)
# --------------------------------------------------------------------------
class Hw:
    def __init__(self) -> None:
        self.gpu_load = self._first_existing([
            "/sys/devices/platform/bus@0/17000000.gpu/load",
            "/sys/devices/platform/17000000.gpu/load",
            "/sys/devices/platform/17000000.ga10b/load",
            "/sys/devices/gpu.0/load",
            "/sys/devices/platform/gpu.0/load",
            *glob.glob("/sys/devices/platform/*/17000000.gpu/load"),
            *glob.glob("/sys/devices/platform/*/*.gpu/load"),
        ])
        gpu_devfreq = [d for d in glob.glob("/sys/class/devfreq/*") if "gpu" in d or "ga10b" in d]
        self.gpu_freq = os.path.join(gpu_devfreq[0], "cur_freq") if gpu_devfreq else None
        self.gpu_max_freq = os.path.join(gpu_devfreq[0], "max_freq") if gpu_devfreq else None
        self.nvidia_smi = shutil.which("nvidia-smi") if not self.gpu_load else None

        self.thermal = []
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            name = _read(os.path.join(z, "type")) or os.path.basename(z)
            self.thermal.append((name.replace("-thermal", "").replace("_", " "), os.path.join(z, "temp")))

        self.power = []  # (label, volt_path, curr_path)
        self.fans = []  # (label, rpm_path|None, pwm_path|None)
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name = _read(os.path.join(hw, "name")) or ""
            if name.startswith("ina3221") or name.startswith("ina238") or name.startswith("ina2"):
                for lab in sorted(glob.glob(os.path.join(hw, "in*_label"))):
                    n = re.search(r"in(\d+)_label", lab).group(1)
                    label = _read(lab) or f"rail{n}"
                    if "sum" in label.lower():
                        continue
                    v = os.path.join(hw, f"in{n}_input")
                    c = os.path.join(hw, f"curr{n}_input")
                    if os.path.exists(v) and os.path.exists(c):
                        self.power.append((label, v, c))
            rpm = glob.glob(os.path.join(hw, "fan*_input"))
            pwm = os.path.join(hw, "pwm1")
            if rpm or (name.startswith("pwm") and os.path.exists(pwm)):
                self.fans.append((name, rpm[0] if rpm else None, pwm if os.path.exists(pwm) else None))

        self.is_jetson = os.path.exists("/etc/nv_tegra_release")
        self.model = (_read("/proc/device-tree/model") or "").strip("\x00") or os.uname().nodename
        self.l4t = (_read("/etc/nv_tegra_release") or "").split("\n")[0]

    @staticmethod
    def _first_existing(paths):
        for p in paths:
            if p and os.path.exists(p):
                return p
        return None


def power_mode() -> dict | None:
    status = _read("/var/lib/nvpmodel/status")
    if not status:
        return None
    m = re.search(r"pmode:(\d+)", status)
    if not m:
        return None
    mode_id = int(m.group(1))
    name = f"Mode {mode_id}"
    conf = _read("/etc/nvpmodel.conf") or ""
    for mm in re.finditer(r"<\s*POWER_MODEL\s+ID=(\d+)\s+NAME=([^\s>]+)", conf):
        if int(mm.group(1)) == mode_id:
            name = mm.group(2)
    return {"id": mode_id, "name": name}


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------
class Collector:
    def __init__(self) -> None:
        self.hw = Hw()
        self.history: deque = deque(maxlen=int(CONFIG["stats"]["history"]))
        self.subscribers: set[asyncio.Queue] = set()
        self.latest: dict | None = None
        self._net_prev = None
        self._disk_prev = None
        self._slow: dict = {}
        self._slow_ts = 0.0
        self._task: asyncio.Task | None = None
        psutil.cpu_percent(percpu=True)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                if time.time() - self._slow_ts > 10:
                    self._slow_ts = time.time()
                    self._slow = await self._slow_sample()
                s = await asyncio.to_thread(self.sample)
                s.update(self._slow)
                from .alerts import engine as _alerts
                s["alerts"] = _alerts.summary()
                if s["gpu"]["load"] is None and s.get("gpu_smi") is not None:
                    s["gpu"]["load"] = s["gpu_smi"]
                self.latest = s
                self.history.append(self._compact(s))
                for q in list(self.subscribers):
                    if q.qsize() < 5:
                        q.put_nowait(s)
            except Exception as e:  # never let the collector die
                print("stats error:", e)
            await asyncio.sleep(CONFIG["stats"]["interval"] if self.subscribers else IDLE_INTERVAL)

    @staticmethod
    def _compact(s: dict) -> dict:
        return {
            "t": s["t"],
            "cpu": s["cpu"]["total"],
            "gpu": s["gpu"]["load"],
            "mem": s["mem"]["percent"],
            "temp": s["temp_max"],
            "power": s["power_total"],
            "rx": s["net_total"]["rx"],
            "tx": s["net_total"]["tx"],
        }

    # fast sample (~1 ms)
    def sample(self) -> dict:
        now = time.time()
        hw = self.hw
        per = psutil.cpu_percent(percpu=True)
        freqs = []
        for i in range(len(per)):
            f = _read_num(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
            freqs.append(round(f / 1000) if f else None)
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        gpu_load = None
        if hw.gpu_load:
            v = _read_num(hw.gpu_load)
            gpu_load = round(v / 10, 1) if v is not None else None
        gpu_freq = _read_num(hw.gpu_freq) if hw.gpu_freq else None

        temps = []
        for name, p in hw.thermal:
            v = _read_num(p)
            if v is not None and -40000 < v < 150000:
                temps.append({"name": name, "c": round(v / 1000, 1)})

        rails = []
        for label, vp, cp in hw.power:
            v, c = _read_num(vp), _read_num(cp)
            if v is not None and c is not None:
                rails.append({"name": label, "mw": round(v * c / 1000), "mv": v, "ma": c})
        vin = [r for r in rails if "VDD_IN" in r["name"].upper()]
        power_total = vin[0]["mw"] if vin else (sum(r["mw"] for r in rails) if rails else None)

        fans = []
        for name, rpm_p, pwm_p in hw.fans:
            rpm = _read_num(rpm_p) if rpm_p else None
            pwm = _read_num(pwm_p) if pwm_p else None
            fans.append({"name": name, "rpm": rpm, "pct": round(pwm / 2.55) if pwm is not None else None})

        # network rates
        nets = psutil.net_io_counters(pernic=True)
        net = []
        tot_rx = tot_tx = 0.0
        if self._net_prev:
            pt, prev = self._net_prev
            dt = max(now - pt, 1e-3)
            for nic, c in nets.items():
                if nic == "lo" or nic not in prev:
                    continue
                rx = (c.bytes_recv - prev[nic].bytes_recv) / dt
                tx = (c.bytes_sent - prev[nic].bytes_sent) / dt
                net.append({"name": nic, "rx": rx, "tx": tx})
                if not nic.startswith("tailscale"):
                    tot_rx += rx
                    tot_tx += tx
        self._net_prev = (now, nets)

        disks = []
        seen = set()
        for p in psutil.disk_partitions(all=False):
            if p.device in seen or p.mountpoint.startswith(("/snap", "/boot/efi")):
                continue
            seen.add(p.device)
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            disks.append({"mount": p.mountpoint, "device": p.device, "fs": p.fstype,
                          "total": u.total, "used": u.used, "percent": u.percent})

        la = os.getloadavg()
        return {
            "t": now,
            "uptime": now - psutil.boot_time(),
            "load": [round(x, 2) for x in la],
            "cpu": {"total": round(sum(per) / len(per), 1), "cores": per, "freq_mhz": freqs},
            "gpu": {"load": gpu_load, "freq_mhz": round(gpu_freq / 1e6) if gpu_freq else None},
            "mem": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
                    "swap_total": sw.total, "swap_used": sw.used},
            "temps": temps,
            "temp_max": max((t["c"] for t in temps), default=None),
            "power": rails,
            "power_total": power_total,
            "fans": fans,
            "net": net,
            "net_total": {"rx": tot_rx, "tx": tot_tx},
            "disks": disks,
        }

    # slow sample every 10 s: subprocess-based things
    async def _slow_sample(self) -> dict:
        from . import services  # local import to avoid cycle

        out: dict = {"power_mode": power_mode()}
        out["failed_services"] = await services.failed_units()
        out["tailscale"] = await tailscale_status()
        if self.hw.nvidia_smi:
            out["gpu_smi"] = await _nvidia_smi_load(self.hw.nvidia_smi)
        return out

    def info(self) -> dict:
        u = os.uname()
        return {
            "hostname": u.nodename,
            "model": self.hw.model,
            "kernel": u.release,
            "l4t": self.hw.l4t,
            "is_jetson": self.hw.is_jetson,
            "os": _os_name(),
            "cpu_count": psutil.cpu_count(),
            "has_gpu_load": bool(self.hw.gpu_load or self.hw.nvidia_smi),
            "has_power": bool(self.hw.power),
        }


def _os_name() -> str:
    txt = _read("/etc/os-release") or ""
    m = re.search(r'PRETTY_NAME="([^"]+)"', txt)
    return m.group(1) if m else "Linux"


async def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except (OSError, asyncio.TimeoutError) as e:
        return 1, "", str(e)


async def _nvidia_smi_load(binary: str):
    rc, out, _ = await _run([binary, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"])
    try:
        return float(out.strip().splitlines()[0]) if rc == 0 else None
    except (ValueError, IndexError):
        return None


async def tailscale_status() -> dict | None:
    if not shutil.which("tailscale"):
        return None
    rc, out, _ = await _run(["tailscale", "status", "--json"])
    if rc != 0:
        return {"online": False}
    try:
        d = json.loads(out)
    except ValueError:
        return None
    self_ = d.get("Self") or {}
    peers = []
    for p in (d.get("Peer") or {}).values():
        peers.append({
            "name": (p.get("HostName") or p.get("DNSName", "").split(".")[0]),
            "os": p.get("OS"),
            "online": bool(p.get("Online")),
            "direct": bool(p.get("CurAddr")),
            "relay": p.get("Relay") or "",
            "ip": (p.get("TailscaleIPs") or [""])[0],
        })
    peers.sort(key=lambda p: (not p["online"], p["name"]))
    return {
        "online": bool(self_.get("Online")),
        "state": d.get("BackendState"),
        "dns": (self_.get("DNSName") or "").rstrip("."),
        "ip": (self_.get("TailscaleIPs") or [""])[0],
        "peers": peers,
    }


collector = Collector()
