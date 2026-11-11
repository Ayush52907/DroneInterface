"""Append-only audit log of every state-changing action (JSON lines)."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from .config import CONFIG

_PATH = os.path.join(CONFIG["state_dir"], "audit.log")
_LOCK = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024


def record(user: dict, action: str, target: str, ok: bool = True, detail: str = "") -> None:
    entry = {
        "ts": time.time(),
        "user": user.get("login", "?"),
        "action": action,
        "target": target,
        "ok": ok,
        "detail": detail[:500],
    }
    with _LOCK:
        try:
            if os.path.exists(_PATH) and os.path.getsize(_PATH) > _MAX_BYTES:
                os.replace(_PATH, _PATH + ".1")
            with open(_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def recent(limit: int = 300) -> list[dict]:
    out: deque = deque(maxlen=limit)
    try:
        with open(_PATH) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return list(reversed(out))
