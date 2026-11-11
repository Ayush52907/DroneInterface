"""Configuration loading. Values in config.yaml override DEFAULTS."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = AGENT_DIR / "web"

DEFAULTS: dict = {
    "server": {"host": "127.0.0.1", "port": 8080,
               "tailscale_direct_port": None},  # e.g. 8080: also listen on the Tailscale IP (plain http, for shared users)
    "auth": {
        # tailscale: trust Tailscale-User-Login header added by `tailscale serve`
        # none:      no identity check (dev only)
        "mode": "tailscale",
        "allowed_users": [],  # empty = every user of the tailnet
    },
    "files": {
        "roots": ["~"],
        "bookmarks": [],
        "max_text_view_bytes": 2 * 1024 * 1024,
    },
    "services": {
        "pinned": [],  # extra units to show under "My services"
        "hidden": [],  # custom units to hide from "My services"
    },
    "stats": {"interval": 1.0, "history": 300},
    "network_targets": [],  # [{name, host, port?, kind: ping|tcp|rtsp, path?}]
    "ros": {"setup": [], "domain_id": None},
    "terminal": {"enabled": True, "keep_alive_minutes": 30},
    "cameras": [],  # [{name, url (rtsp://... or "test"), codec: h264|h265}]
    "camera_preview": {"max_minutes": 5},
    "gimbal": {"enabled": True, "service": "siyi-pitch.service", "flight_script": "~/siyi-python-sdk/pitch_down.py",
               "flight_pitch": -90, "tolerance": 5, "yaw_tolerance": 5, "host": "", "port": 37260},
    "flight_data": {"session_gap_minutes": 10, "sources": []},
    "alerts": {
        "check_seconds": 5,
        "camera_lost": {"enabled": True},
        "usb_ttl": {"enabled": True, "devices": [], "flap_count": 3, "flap_minutes": 5},
        "port_conflict": {"enabled": True},
        "gimbal": {"enabled": True, "after_seconds": 10},
    },
    "state_dir": "~/.local/state/drone-panel",
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    path = Path(os.environ.get("DRONE_PANEL_CONFIG", AGENT_DIR / "config.yaml"))
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    cfg = _merge(DEFAULTS, data)
    # note: a bare `~` in YAML is null, so treat null/empty as the home folder
    roots = cfg["files"]["roots"] or ["~"]
    if isinstance(roots, str):
        roots = [roots]
    cfg["files"]["roots"] = [
        os.path.realpath(os.path.expanduser(str(r) if r else "~")) for r in roots
    ]
    cfg["files"]["bookmarks"] = [
        {"name": b.get("name") or b["path"], "path": os.path.expanduser(str(b["path"]))}
        for b in (cfg["files"]["bookmarks"] or []) if b and b.get("path")
    ]
    for key in ("pinned", "hidden"):
        cfg["services"][key] = [str(u) for u in (cfg["services"][key] or []) if u]
    cfg["auth"]["allowed_users"] = [str(u) for u in (cfg["auth"]["allowed_users"] or []) if u]
    cfg["network_targets"] = [t for t in (cfg["network_targets"] or []) if t and t.get("host")]
    cfg["ros"]["setup"] = [os.path.expanduser(str(p)) for p in (cfg["ros"]["setup"] or []) if p]
    if not cfg["ros"]["setup"]:  # auto-detect newest ROS 2 distro
        import glob as _g
        found = sorted(_g.glob("/opt/ros/*/setup.bash"))
        cfg["ros"]["setup"] = found[-1:]
    cfg["cameras"] = [c for c in (cfg["cameras"] or []) if c and c.get("url")]
    cfg["flight_data"]["sources"] = [s for s in (cfg["flight_data"]["sources"] or []) if s and s.get("folders")]
    cfg["state_dir"] = os.path.expanduser(cfg["state_dir"])
    os.makedirs(cfg["state_dir"], exist_ok=True)
    if os.environ.get("DRONE_PANEL_AUTH"):
        cfg["auth"]["mode"] = os.environ["DRONE_PANEL_AUTH"]
    return cfg


CONFIG = load()
