"""drone.yaml: the single file for drone-specific settings.

Holds the flight-controller connection, mavlink-router, payload servos and
preflight thresholds. From it the panel generates (on "apply"):
  * /etc/mavlink-router/main.conf          (via dpctl, root-owned)
  * ~/drone-env/<client>.env               MAVLINK_PORT / MAVLINK_BAUD per program,
                                           for services to load with EnvironmentFile=
so changing e.g. the FC port is one edit here.
"""
from __future__ import annotations

import copy
import ipaddress
import os
import re
import subprocess
import threading

import yaml
from fastapi import HTTPException

from .config import CONFIG

PATH = os.path.expanduser(CONFIG.get("drone_profile") or "~/drone.yaml")
ENV_DIR = os.path.expanduser("~/drone-env")
DPCTL = "/opt/drone-panel/bin/dpctl"
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None

# Seeded from drop_drop.py / drop_scout.py (FC on /dev/ttyTHS1 @ 115200,
# servos 11/12 with Low 1100 / Mid 1500 / High 1900).
DEFAULT = {
    "fc": {"port": "/dev/ttyTHS1", "baud": 115200},
    "mavlink_router": {
        "enabled": False,
        "clients": {"panel": 14551, "drop_scout": 14550, "drop_drop": 14552, "spare": 14553, "rudra_sender": 14554},
    },
    "payload": [
        {"name": "Servo 12 (payloads 1 & 2)", "servo": 12, "return_after": 0, "positions": [
            {"name": "Low (release payload 1)", "pwm": 1100, "kind": "release"},
            {"name": "Mid (both locked)", "pwm": 1500, "kind": "rest"},
            {"name": "High (release payload 2)", "pwm": 1900, "kind": "release"}]},
        {"name": "Servo 11 (payloads 3 & 4)", "servo": 11, "return_after": 0, "positions": [
            {"name": "Low (release payload 3)", "pwm": 1100, "kind": "release"},
            {"name": "Mid (both locked)", "pwm": 1500, "kind": "rest"},
            {"name": "High (release payload 4)", "pwm": 1900, "kind": "release"}]},
    ],
    "preflight": {"rc_min_quality": 70, "gps_min_fix": 3, "gps_min_sats": 10, "gps_max_hdop": 1.4, "link_max_loss": 2.0},
}
BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 500000, 921600, 1500000)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ---------------------------------------------------------------- validation
def validate(d: dict) -> dict:
    if not isinstance(d, dict):
        raise HTTPException(400, "drone.yaml must be a mapping")
    out = copy.deepcopy(DEFAULT)
    fc = d.get("fc") or {}
    port = str(fc.get("port", out["fc"]["port"])).strip()
    if not re.fullmatch(r"/dev/[A-Za-z0-9_./-]+", port) or ".." in port:
        raise HTTPException(400, f"fc.port must be a device like /dev/ttyTHS1 (got {port!r})")
    try:
        baud = int(fc.get("baud", out["fc"]["baud"]))
    except (TypeError, ValueError):
        baud = 0
    if baud not in BAUDS:
        raise HTTPException(400, f"fc.baud {fc.get('baud')!r} is not a standard rate ({', '.join(map(str, BAUDS))})")
    out["fc"] = {"port": port, "baud": baud}

    mr = d.get("mavlink_router") or {}
    clients = mr.get("clients", out["mavlink_router"]["clients"]) or {}
    if not isinstance(clients, dict):
        raise HTTPException(400, "mavlink_router.clients must be a mapping of name: port")
    clean_clients, used = {}, set()
    for name, p in clients.items():
        if not NAME_RE.match(str(name)):
            raise HTTPException(400, f"client name {name!r}: letters, digits, _ and - only")
        try:
            p = int(p)
        except (TypeError, ValueError):
            raise HTTPException(400, f"client {name}: port must be a number")
        if not 1024 <= p <= 65535 or p in used:
            raise HTTPException(400, f"client {name}: port {p} must be 1024-65535 and unique")
        used.add(p)
        clean_clients[str(name)] = p
    if "panel" not in clean_clients:
        raise HTTPException(400, "mavlink_router.clients needs a 'panel' entry (the panel's own port)")
    gcs = mr.get("gcs") or []
    clean_gcs = []
    for g in gcs:
        try:
            ipaddress.ip_address(str(g.get("address")))
            clean_gcs.append({"address": str(g["address"]), "port": int(g.get("port", 14550))})
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, f"mavlink_router.gcs entry {g!r}: needs a valid address and port")
    out["mavlink_router"] = {"enabled": bool(mr.get("enabled", False)), "clients": clean_clients, "gcs": clean_gcs}

    servos = d.get("payload", out["payload"]) or []
    clean_servos = []
    for c in servos:
        name = str(c.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "every payload entry needs a name")
        try:
            servo = int(c.get("servo"))
        except (TypeError, ValueError):
            raise HTTPException(400, f"payload {name}: servo must be a number")
        if not 1 <= servo <= 32:
            raise HTTPException(400, f"payload {name}: servo must be 1-32")
        positions = []
        for p in c.get("positions") or []:
            pn = str(p.get("name") or "").strip()
            try:
                pwm = int(p.get("pwm"))
            except (TypeError, ValueError):
                raise HTTPException(400, f"payload {name}: PWM must be a number")
            if not pn or not 500 <= pwm <= 2500:
                raise HTTPException(400, f"payload {name}: every position needs a name and a PWM of 500-2500")
            positions.append({"name": pn, "pwm": pwm, "kind": "release" if p.get("kind") == "release" else "rest"})
        if not positions:
            raise HTTPException(400, f"payload {name}: add at least one position")
        try:
            ret = max(0.0, min(float(c.get("return_after") or 0), 120.0))
        except (TypeError, ValueError):
            ret = 0.0
        clean_servos.append({"name": name, "servo": servo, "return_after": ret, "positions": positions})
    out["payload"] = clean_servos

    pf = {**out["preflight"], **(d.get("preflight") or {})}
    try:
        out["preflight"] = {"rc_min_quality": max(0, min(100, int(pf["rc_min_quality"]))),
                            "gps_min_fix": max(2, min(6, int(pf["gps_min_fix"]))),
                            "gps_min_sats": max(0, min(40, int(pf["gps_min_sats"]))),
                            "gps_max_hdop": max(0.5, min(10.0, float(pf["gps_max_hdop"]))),
                            "link_max_loss": max(0.0, min(100.0, float(pf["link_max_loss"])))}
    except (TypeError, ValueError, KeyError):
        raise HTTPException(400, "preflight thresholds must be numbers")
    return out


# ---------------------------------------------------------------- file I/O
def render(d: dict) -> str:
    """drone.yaml with explanatory comments (regenerated on every save)."""
    y = lambda o: yaml.safe_dump(o, sort_keys=False, default_flow_style=None, allow_unicode=True, width=120)
    mr = d["mavlink_router"]
    return f"""# drone.yaml: the one file for this drone's important settings.
# Edit it here or in the panel (Settings page), then press "Save & apply".
# The panel regenerates /etc/mavlink-router/main.conf and ~/drone-env/*.env from it.

# Flight controller serial link (the FC's TELEM port wired to the Jetson)
fc:
  port: {d['fc']['port']}      # e.g. /dev/ttyTHS1 (Jetson UART) or /dev/ttyUSB0 (USB-TTL)
  baud: {d['fc']['baud']}

# mavlink-router owns the FC port and shares it with several programs over local UDP.
#   enabled: false -> programs open fc.port directly (only one at a time!)
#   enabled: true  -> mavlink-router runs; each client gets udpin:127.0.0.1:<port>
# Services load their setting from ~/drone-env/<client>.env (MAVLINK_PORT, MAVLINK_BAUD).
mavlink_router:
  enabled: {'true' if mr['enabled'] else 'false'}
  clients:      # name: local UDP port ('panel' is the panel itself)
{''.join(f'    {k}: {v}{chr(10)}' for k, v in mr['clients'].items())}  gcs: {y(mr['gcs']).strip()}   # optional ground stations, e.g. [{{address: 100.x.y.z, port: 14550}}]

# Payload servos on the FC outputs (Payload page). kind: rest | release (asks to confirm)
payload:
{''.join('  ' + line + chr(10) for line in y(d['payload']).splitlines())}
# Preflight thresholds (Preflight page: RC link, GPS fix, satellites, HDOP)
preflight:
  rc_min_quality: {d['preflight']['rc_min_quality']}   # % RC link quality (ELRS), if the FC reports it
  gps_min_fix: {d['preflight']['gps_min_fix']}        # 3 = 3D fix, 4 = DGPS, 5 = RTK float, 6 = RTK fixed
  gps_min_sats: {d['preflight']['gps_min_sats']}
  gps_max_hdop: {d['preflight']['gps_max_hdop']}
  link_max_loss: {d['preflight']['link_max_loss']}   # % lost FC packets allowed by the FC <-> OBC link check
"""


def _migrate_payload(d: dict) -> dict:
    """Earlier versions kept payload servos in ~/.local/state/drone-panel/payload.json."""
    import json
    old = os.path.join(CONFIG["state_dir"], "payload.json")
    try:
        p = json.load(open(old))
        d["payload"] = [{k: v for k, v in c.items() if k != "id"} for c in p.get("configs", [])] or d["payload"]
        for c in d["payload"]:
            c["positions"] = [{k: v for k, v in pos.items() if k != "id"} for pos in c["positions"]]
        os.replace(old, old + ".migrated")
    except (OSError, ValueError):
        pass
    return d


def load() -> dict:
    global _cache
    with _lock:
        try:
            mt = os.path.getmtime(PATH)
        except OSError:
            d = _migrate_payload(copy.deepcopy(DEFAULT))
            _write(validate(d))
            mt = os.path.getmtime(PATH)
        if _cache and _cache[0] == mt:
            return copy.deepcopy(_cache[1])
        try:
            d = validate(yaml.safe_load(open(PATH)) or {})
        except HTTPException as e:
            # keep running on the last good version; the Settings page shows the error
            if _cache:
                return copy.deepcopy(_cache[1])
            raise HTTPException(500, f"drone.yaml is invalid: {e.detail}")
        except yaml.YAMLError as e:
            if _cache:
                return copy.deepcopy(_cache[1])
            raise HTTPException(500, f"drone.yaml has a syntax error: {e}")
        _cache = (mt, d)
        return copy.deepcopy(d)


def _write(d: dict) -> None:
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(render(d))
    os.replace(tmp, PATH)


def raw() -> dict:
    try:
        text = open(PATH).read()
    except OSError:
        load()
        text = open(PATH).read()
    err = None
    try:
        validate(yaml.safe_load(text) or {})
    except HTTPException as e:
        err = e.detail
    except yaml.YAMLError as e:
        err = f"YAML syntax error: {e}"
    return {"path": PATH, "text": text, "error": err, "mtime": os.path.getmtime(PATH)}


def save_text(text: str) -> dict:
    try:
        d = validate(yaml.safe_load(text) or {})
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML syntax error: {e}")
    with open(PATH + ".tmp", "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.replace(PATH + ".tmp", PATH)
    return d


def save_section(key: str, value) -> dict:
    d = load()
    d[key] = value
    d = validate(d)
    _write(d)
    return d


# ---------------------------------------------------------------- generated files
def router_conf(d: dict) -> str:
    mr = d["mavlink_router"]
    lines = ["# Generated by Drone Panel from ~/drone.yaml (edit that file, not this one)",
             "[General]", "TcpServerPort=0", "ReportStats=false", "MavlinkDialect=auto",
             "# the panel registers system id 253 to see all FC traffic (FC <-> OBC link-quality check)",
             "SnifferSysid=253", "",
             "[UartEndpoint fc]", f"Device={d['fc']['port']}", f"Baud={d['fc']['baud']}", ""]
    for name, port in mr["clients"].items():
        lines += [f"[UdpEndpoint {name}]", "Mode=Normal", "Address=127.0.0.1", f"Port={port}", ""]
    for i, g in enumerate(mr["gcs"]):
        lines += [f"[UdpEndpoint gcs{i + 1}]", "Mode=Normal", f"Address={g['address']}", f"Port={g['port']}", ""]
    return "\n".join(lines)


def client_url(d: dict, name: str) -> tuple[str, int]:
    """(MAVLINK_PORT, MAVLINK_BAUD) a program should use."""
    if d["mavlink_router"]["enabled"]:
        return f"udpin:127.0.0.1:{d['mavlink_router']['clients'][name]}", d["fc"]["baud"]
    return d["fc"]["port"], d["fc"]["baud"]


def write_env_files(d: dict) -> list[str]:
    os.makedirs(ENV_DIR, exist_ok=True)
    written = []
    for name in d["mavlink_router"]["clients"]:
        if name == "panel":
            continue
        url, baud = client_url(d, name)
        p = os.path.join(ENV_DIR, f"{name}.env")
        with open(p + ".tmp", "w") as f:
            f.write(f"# Generated by Drone Panel from ~/drone.yaml; load with EnvironmentFile={p}\n"
                    f"MAVLINK_PORT={url}\nMAVLINK_BAUD={baud}\n")
        os.replace(p + ".tmp", p)
        written.append(p)
    return written


def _run(cmd: list[str], stdin: str | None = None, timeout: float = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def apply(d: dict | None = None) -> list[str]:
    """Regenerate files and start/stop mavlink-router to match drone.yaml."""
    from . import hardware
    d = d or load()
    log = []
    for p in write_env_files(d):
        log.append(f"wrote {p.replace(os.path.expanduser('~'), '~')}")
    if not os.path.exists(DPCTL):
        log.append("dpctl not installed (development machine): mavlink-router not configured")
        return log
    rc, out = _run(["sudo", "-n", DPCTL, "router-config"], stdin=router_conf(d))
    log.append(out or ("wrote /etc/mavlink-router/main.conf" if rc == 0 else "failed to write router config"))
    if rc != 0:
        raise HTTPException(500, "\n".join(log))
    if d["mavlink_router"]["enabled"]:
        # refuse if another program holds the FC port directly: both would read it -> corruption
        real = os.path.realpath(d["fc"]["port"])
        owners, _ = hardware.device_owners({real})
        others = [o for o in owners.get(real, []) if "mavlink-router" not in (o["comm"] + (o["unit"] or ""))]
        if others:
            names = ", ".join(sorted({(o["unit"] or o["comm"]).replace(".service", "") for o in others}))
            log.append(f"NOT starting mavlink-router: {d['fc']['port']} is open in {names}. "
                       f"Switch those services to ~/drone-env/<name>.env and restart them first.")
            raise HTTPException(409, "\n".join(log))
        for act in ("enable", "restart"):
            rc, out = _run(["sudo", "-n", DPCTL, act, "mavlink-router.service"])
            log.append(f"mavlink-router {act}: {'ok' if rc == 0 else out}")
    else:
        for act in ("stop", "disable"):
            rc, out = _run(["sudo", "-n", DPCTL, act, "mavlink-router.service"])
            log.append(f"mavlink-router {act}: {'ok' if rc == 0 else out}")
    return log


def router_status() -> dict:
    rc, out = _run(["systemctl", "show", "mavlink-router.service", "-p", "ActiveState,UnitFileState,LoadState"], timeout=5)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"installed": props.get("LoadState") == "loaded", "active": props.get("ActiveState"),
            "enabled": props.get("UnitFileState")}


def services_using_env(d: dict) -> list[dict]:
    """Which systemd units already load ~/drone-env/<client>.env."""
    out = []
    for name in d["mavlink_router"]["clients"]:
        if name == "panel":
            continue
        unit = f"{name}.service"
        rc, txt = _run(["systemctl", "cat", "--no-pager", unit], timeout=5)
        exists = rc == 0
        uses = exists and os.path.join(ENV_DIR, f"{name}.env") in txt
        out.append({"client": name, "unit": unit, "exists": exists, "uses_env": uses})
    return out
