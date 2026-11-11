"""systemd service management via systemctl.

"My services" = unit files that are NOT owned by any .deb package (i.e. ones
you created yourself), plus anything listed in services.pinned.
Privileged actions go through /opt/drone-panel/bin/dpctl via sudo (see install.sh).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time

from .config import CONFIG

UNIT_DIRS = ["/etc/systemd/system", "/usr/local/lib/systemd/system",
             "/lib/systemd/system", "/usr/lib/systemd/system"]
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]+\.service$")
ACTIONS = {"start", "stop", "restart", "enable", "disable", "reset-failed"}
PROPS = ["Id", "Description", "LoadState", "ActiveState", "SubState", "UnitFileState",
         "MainPID", "ActiveEnterTimestampMonotonic", "InactiveEnterTimestampMonotonic",
         "NRestarts", "MemoryCurrent", "CPUUsageNSec", "FragmentPath", "Result",
         "ExecMainStatus", "User", "WorkingDirectory", "Restart", "TasksCurrent"]


def _self_unit() -> str | None:
    try:
        with open("/proc/self/cgroup") as f:
            path = f.read().strip().split(":")[-1]
        if "user.slice" in path:
            return None
        last = path.rstrip("/").split("/")[-1]
        return last if last.endswith(".service") else None
    except OSError:
        return None


SELF_UNIT = _self_unit()


def norm_unit(name: str) -> str:
    if not name.endswith(".service"):
        name += ".service"
    if not UNIT_RE.match(name):
        raise ValueError(f"invalid unit name: {name}")
    return name


async def run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "", "LANG": "C.UTF-8"})
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "", "timed out"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# ---------------------------------------------------------------- package-owned files
_owned_cache: tuple[float, set[str]] | None = None


def _package_owned() -> set[str]:
    global _owned_cache
    info = "/var/lib/dpkg/info"
    try:
        mtime = os.stat(info).st_mtime
    except OSError:
        return set()
    if _owned_cache and _owned_cache[0] == mtime:
        return _owned_cache[1]
    owned: set[str] = set()
    for lst in glob.glob(os.path.join(info, "*.list")):
        try:
            with open(lst, errors="replace") as f:
                for line in f:
                    if "systemd/system/" in line and line.rstrip().endswith(".service"):
                        p = line.rstrip()
                        owned.add(os.path.basename(p))
        except OSError:
            continue
    _owned_cache = (mtime, owned)
    return owned


VIEW_PATH = os.path.join(CONFIG["state_dir"], "services_view.json")


def load_view() -> dict:
    try:
        with open(VIEW_PATH) as f:
            v = json.load(f)
    except (OSError, ValueError):
        v = {}
    return {"pinned": list(v.get("pinned", [])), "hidden": list(v.get("hidden", []))}


def set_view(unit: str, mode: str) -> dict:
    """mode: hide (remove from My services), pin (add to My services), reset (automatic)."""
    unit = norm_unit(unit)
    if mode not in ("hide", "pin", "reset"):
        raise ValueError("mode must be hide, pin or reset")
    v = load_view()
    v["pinned"] = [u for u in v["pinned"] if u != unit]
    v["hidden"] = [u for u in v["hidden"] if u != unit]
    if mode == "pin":
        v["pinned"].append(unit)
    elif mode == "hide":
        v["hidden"].append(unit)
    tmp = VIEW_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(v, f, indent=2)
    os.replace(tmp, VIEW_PATH)
    return v


def auto_unit_names() -> set[str]:
    owned = _package_owned()
    found: set[str] = set()
    for d in UNIT_DIRS:
        for path in glob.glob(os.path.join(d, "*.service")):
            name = os.path.basename(path)
            if name in owned or name.startswith(("snap.", "snap-")):
                continue
            if os.path.islink(path):  # enablement/alias symlinks, /dev/null masks
                continue
            found.add(name)
    return found


def custom_unit_names() -> set[str]:
    view = load_view()
    found = auto_unit_names()
    found |= {norm_unit(u) for u in CONFIG["services"]["pinned"]} | set(view["pinned"])
    found -= {norm_unit(u) for u in CONFIG["services"]["hidden"]} | set(view["hidden"])
    return found


# ---------------------------------------------------------------- queries
def _parse_show(text: str) -> list[dict]:
    units, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                units.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        units.append(cur)
    return units


_cpu_prev: dict[str, tuple[float, int]] = {}


def _enrich(p: dict) -> dict:
    now_mono = time.monotonic()
    name = p.get("Id", "")

    def num(k):
        try:
            v = int(p.get(k, ""))
            return None if v >= 2**63 - 1 or v == 18446744073709551615 else v
        except ValueError:
            return None

    active_since = num("ActiveEnterTimestampMonotonic")
    uptime = (now_mono - active_since / 1e6) if active_since and p.get("ActiveState") == "active" else None
    cpu_ns = num("CPUUsageNSec")
    cpu_pct = None
    if cpu_ns is not None:
        prev = _cpu_prev.get(name)
        if prev and now_mono > prev[0]:
            cpu_pct = round(max(0, cpu_ns - prev[1]) / 1e9 / (now_mono - prev[0]) * 100, 1)
        _cpu_prev[name] = (now_mono, cpu_ns)
    return {
        "name": name,
        "description": p.get("Description", ""),
        "load": p.get("LoadState"),
        "active": p.get("ActiveState"),
        "sub": p.get("SubState"),
        "enabled": p.get("UnitFileState") or "",
        "pid": num("MainPID") or None,
        "uptime": uptime,
        "restarts": num("NRestarts"),
        "memory": num("MemoryCurrent"),
        "cpu": cpu_pct,
        "tasks": num("TasksCurrent"),
        "path": p.get("FragmentPath", ""),
        "result": p.get("Result"),
        "exit_status": p.get("ExecMainStatus"),
        "user": p.get("User") or "root",
        "workdir": p.get("WorkingDirectory", ""),
        "restart_policy": p.get("Restart"),
        "self": name == SELF_UNIT,
    }


async def show(units: list[str]) -> list[dict]:
    if not units:
        return []
    rc, out, err = await run(["systemctl", "show", "--no-pager", "-p", ",".join(PROPS), *units])
    return [_enrich(p) for p in _parse_show(out) if p.get("Id")]


async def list_all() -> dict:
    (rc1, units_json, _), (rc2, files_json, _) = await asyncio.gather(
        run(["systemctl", "list-units", "--type=service", "--all", "--output=json", "--no-pager"]),
        run(["systemctl", "list-unit-files", "--type=service", "--output=json", "--no-pager"]),
    )
    try:
        units = json.loads(units_json) if rc1 == 0 else []
    except ValueError:
        units = []
    try:
        files = json.loads(files_json) if rc2 == 0 else []
    except ValueError:
        files = []
    enabled = {f["unit_file"]: f.get("state", "") for f in files}
    seen = set()
    system = []
    for u in units:
        name = u["unit"]
        seen.add(name)
        system.append({"name": name, "description": u.get("description", ""), "load": u.get("load"),
                       "active": u.get("active"), "sub": u.get("sub"), "enabled": enabled.get(name, "")})
    for name, state in enabled.items():  # installed but never loaded
        if name not in seen and not name.endswith("@.service"):
            system.append({"name": name, "description": "", "load": "not-loaded", "active": "inactive",
                           "sub": "dead", "enabled": state})

    custom_names = sorted(custom_unit_names())
    templates = [n for n in custom_names if n.endswith("@.service")]
    # include running instances of custom templates
    for u in units:
        base = re.sub(r"@.+\.service$", "@.service", u["unit"])
        if base in templates and u["unit"] not in custom_names:
            custom_names.append(u["unit"])
    custom = await show([n for n in custom_names if not n.endswith("@.service")])
    view = load_view()
    pinned = set(view["pinned"]) | {norm_unit(u) for u in CONFIG["services"]["pinned"]}
    auto = auto_unit_names()
    for c in custom:
        c["origin"] = "auto" if c["name"] in auto else "pinned"
    custom_set = {c["name"] for c in custom}
    hidden_set = set(view["hidden"]) | {norm_unit(u) for u in CONFIG["services"]["hidden"]}
    hidden = [s for s in system if s["name"] in hidden_set]
    system = [s for s in system if s["name"] not in custom_set]
    for s_ in system:
        s_["hidden"] = s_["name"] in hidden_set
    system.sort(key=lambda s: s["name"])
    return {"custom": custom, "system": system, "hidden": sorted(hidden, key=lambda s: s["name"]),
            "self": SELF_UNIT, "pinned": sorted(pinned)}


async def failed_units() -> list[str]:
    rc, out, _ = await run(["systemctl", "list-units", "--type=service", "--state=failed",
                            "--output=json", "--no-pager"], timeout=5)
    try:
        return [u["unit"] for u in json.loads(out)] if rc == 0 else []
    except ValueError:
        return []


async def detail(unit: str) -> dict:
    unit = norm_unit(unit)
    info, (rc, cat, cat_err), (_, logs, _) = await asyncio.gather(
        show([unit]),
        run(["systemctl", "cat", "--no-pager", unit]),
        run(["journalctl", "-u", unit, "-n", "50", "--no-pager", "-o", "short-iso"]),
    )
    return {
        "info": info[0] if info else None,
        "unit_file": cat if rc == 0 else cat_err.strip(),
        "logs": logs,
    }


DPCTL = "/opt/drone-panel/bin/dpctl"


async def action(unit: str, act: str) -> tuple[bool, str]:
    unit = norm_unit(unit)
    if act not in ACTIONS:
        raise ValueError(f"invalid action {act}")
    if os.path.exists(DPCTL):  # installed: the restricted root helper
        rc, out, err = await run(["sudo", "-n", DPCTL, act, unit])
    else:  # development machine
        rc, out, err = await run(["systemctl", "--no-ask-password", act, unit])
    return rc == 0, (out + err).strip()


# ---------------------------------------------------------------- unit file editor
async def unit_file(unit: str) -> dict:
    unit = norm_unit(unit)
    info = await show([unit])
    path = info[0]["path"] if info else ""
    editable, reason = True, ""
    if not path:
        editable, reason = False, "unit file not found"
    elif not path.startswith("/etc/systemd/system/") or os.path.islink(path):
        editable, reason = False, f"{path} is a system/package unit file"
    elif os.path.basename(path) in _package_owned():
        editable, reason = False, "this unit belongs to an installed package"
    try:
        content = open(path).read() if path else ""
    except OSError as e:
        content, editable, reason = "", False, str(e)
    return {"unit": unit, "path": path, "content": content, "editable": editable, "reason": reason,
            "installed": os.path.exists(DPCTL)}


async def verify(name: str, content: str) -> dict:
    import tempfile
    name = norm_unit(name)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(content)
        rc, out, err = await run(["systemd-analyze", "verify", "--man=no", p], timeout=20)
    text = (out + err).replace(p, name).strip()
    # ignore complaints about *other* units systemd happens to load
    lines = [ln for ln in text.splitlines() if name in ln]
    return {"ok": not lines, "output": "\n".join(lines) or "No problems found."}


async def write_unit(name: str, content: str) -> tuple[bool, str]:
    name = norm_unit(name)
    if not os.path.exists(DPCTL):
        return False, "service editor needs the installed panel (dpctl)"
    p = await asyncio.create_subprocess_exec("sudo", "-n", DPCTL, "unit-write", name, stdin=asyncio.subprocess.PIPE,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(content.encode()), 30)
    return p.returncode == 0, (out + err).decode(errors="replace").strip()


async def delete_unit(name: str) -> tuple[bool, str]:
    name = norm_unit(name)
    if not os.path.exists(DPCTL):
        return False, "service editor needs the installed panel (dpctl)"
    rc, out, err = await run(["sudo", "-n", DPCTL, "unit-delete", name], timeout=60)
    set_view(name, "reset") if name in load_view()["hidden"] + load_view()["pinned"] else None
    return rc == 0, (out + err).strip()


async def exists(name: str) -> bool:
    info = await show([norm_unit(name)])
    return bool(info) and info[0]["load"] != "not-found"
