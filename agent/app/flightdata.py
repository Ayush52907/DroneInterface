"""Flight data manager: groups pipeline output into runs / sessions.

Two kinds of source (config: flight_data.sources):
  mode: runs      folders that share numbered run subfolders, e.g.
                  geo_gps/4, geo_rtsp/4, geo_calc/4, geo_dets/4  -> "Run 4"
  mode: sessions  flat folders; files are split into sessions wherever there
                  is a gap of more than session_gap_minutes between files
A source may also list individual result files (e.g. targets_clustered.txt)
that each run overwrites: shown as "latest results" and zipped with the newest run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

from fastapi import HTTPException

from . import files
from .config import CONFIG

IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|bmp)$", re.I)
VIDEO_RE = re.compile(r"\.(mp4|mkv|avi|mov|h264|h265|ts)$", re.I)
NOTES_PATH = os.path.join(CONFIG["state_dir"], "flightdata_notes.json")
CACHE_TTL = 15
_cache: dict = {}
_lock = threading.Lock()


SOURCES_PATH = os.path.join(CONFIG["state_dir"], "flightdata_sources.json")


def _raw_sources() -> tuple[list[dict], str]:
    """Sources edited in the panel win; config.yaml only provides the defaults."""
    try:
        with open(SOURCES_PATH) as f:
            return json.load(f), "panel"
    except (OSError, ValueError):
        return list(CONFIG["flight_data"]["sources"] or []), "config"


def sources() -> list[dict]:
    out = []
    for i, s in enumerate(_raw_sources()[0]):
        folders = [os.path.realpath(os.path.expanduser(str(f))) for f in (s.get("folders") or [])]
        extra = [os.path.realpath(os.path.expanduser(str(f))) for f in (s.get("files") or [])]
        out.append({"idx": i, "name": s.get("name") or f"Source {i + 1}", "mode": s.get("mode", "sessions"),
                    "folders": folders, "files": extra})
    return out


def _pretty(p: str) -> str:
    home = os.path.realpath(os.path.expanduser("~"))
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def editable_sources() -> dict:
    raw, origin = _raw_sources()
    pretty = _pretty
    return {"origin": origin, "sources": [
        {"name": s.get("name", ""), "mode": s.get("mode", "sessions"),
         "folders": [pretty(os.path.realpath(os.path.expanduser(str(f)))) for f in s.get("folders") or []],
         "files": [pretty(os.path.realpath(os.path.expanduser(str(f)))) for f in s.get("files") or []]} for s in raw]}


def save_sources(items: list[dict]) -> dict:
    clean, names = [], set()
    for s in items:
        name = (s.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "every data source needs a name")
        if name.lower() in names:
            raise HTTPException(400, f"duplicate name: {name}")
        names.add(name.lower())
        mode = s.get("mode")
        if mode not in ("runs", "sessions"):
            raise HTTPException(400, f"{name}: type must be runs or sessions")
        folders, extra = [], []
        for f in list(s.get("folders") or []) + list(s.get("files") or []):
            f = str(f).strip()
            if not f:
                continue
            p = files.safe(f)  # must exist and be inside the allowed folders
            target = folders if os.path.isdir(p) else extra
            if p not in target:
                target.append(p)
        if not folders and not extra:
            raise HTTPException(400, f"{name}: add at least one folder or file")
        clean.append({"name": name, "mode": mode, "folders": folders, "files": extra})
    with open(SOURCES_PATH + ".tmp", "w") as fh:
        json.dump(clean, fh, indent=2)
    os.replace(SOURCES_PATH + ".tmp", SOURCES_PATH)
    with _lock:
        _cache.clear()
    return editable_sources()


def reset_sources() -> dict:
    try:
        os.unlink(SOURCES_PATH)
    except OSError:
        pass
    with _lock:
        _cache.clear()
    return editable_sources()


def folder_candidates() -> list[dict]:
    """Folders in the home folder (2 levels) with a hint of what they contain."""
    home = files.ROOTS[0]
    out = []

    def describe(p: str) -> dict | None:
        try:
            entries = list(os.scandir(p))
        except OSError:
            return None
        numbered = sum(1 for e in entries if e.is_dir() and e.name.isdigit())
        nfiles = sum(1 for e in entries if not e.name.startswith(".") and e.is_file())
        imgs = sum(1 for e in entries if IMAGE_RE.search(e.name))
        if not numbered and not nfiles:
            return None
        return {"path": _pretty(p), "numbered": numbered, "files": nfiles, "images": imgs,
                "suggest": "runs" if numbered else "sessions"}

    skip = {"site-packages", "node_modules", "build", "install", "log", "__pycache__"}
    try:
        top = sorted(e.path for e in os.scandir(home) if e.is_dir() and not e.name.startswith(".")
                     and e.name not in skip and "venv" not in e.name)
    except OSError:
        top = []
    for d in top:
        info = describe(d)
        if info:
            out.append(info)
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                if e.is_dir() and not e.name.startswith(".") and not e.name.isdigit() and e.name not in skip:
                    sub = describe(e.path)
                    if sub and (sub["numbered"] or sub["files"] >= 5):
                        out.append(sub)
        except OSError:
            pass
    return out[:300]


def _source(idx: int) -> dict:
    src = sources()
    if not 0 <= idx < len(src):
        raise HTTPException(404, "unknown data source")
    return src[idx]


def _files_in(d: str, recursive: bool):
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        yield e.path, st.st_size, st.st_mtime
                    elif recursive and e.is_dir(follow_symlinks=False):
                        yield from _files_in(e.path, True)
                except OSError:
                    continue
    except OSError:
        return


def _summary(item_id: str, label: str, entries: list[tuple[str, str, int, float]]) -> dict:
    """entries: (folder, path, size, mtime)"""
    by: dict[str, dict] = {}
    for folder, p, size, mt in entries:
        b = by.setdefault(folder, {"folder": os.path.basename(folder), "count": 0, "size": 0})
        b["count"] += 1
        b["size"] += size
    mts = [e[3] for e in entries]
    return {
        "id": item_id, "label": label,
        "start": min(mts) if mts else None, "end": max(mts) if mts else None,
        "count": len(entries), "size": sum(e[2] for e in entries),
        "images": sum(1 for e in entries if IMAGE_RE.search(e[1])),
        "videos": sum(1 for e in entries if VIDEO_RE.search(e[1])),
        "by_folder": list(by.values()),
    }


def _scan(src: dict) -> tuple[list[dict], dict[str, list]]:
    """Returns (summaries, id -> entries)."""
    groups: dict[str, list] = {}
    if src["mode"] == "runs":
        for folder in src["folders"]:
            try:
                subs = [e for e in os.scandir(folder) if e.is_dir() and not e.name.startswith(".")]
            except OSError:
                continue
            for sub in subs:
                g = groups.setdefault(sub.name, [])
                g.extend((folder, p, sz, mt) for p, sz, mt in _files_in(sub.path, True))
                g.append((folder, sub.path, -1, 0))  # remember the run folder itself (for delete)
        items = []
        for rid, entries in groups.items():
            real = [e for e in entries if e[2] >= 0]
            s = _summary(rid, f"Run {rid}", real)
            s["empty"] = not real
            items.append(s)
        items.sort(key=lambda s: (s["start"] or 0, int(s["id"]) if s["id"].isdigit() else 0), reverse=True)
        return items, groups
    # sessions
    gap = float(CONFIG["flight_data"]["session_gap_minutes"]) * 60
    allf = sorted(((folder, p, sz, mt) for folder in src["folders"] for p, sz, mt in _files_in(folder, False)),
                  key=lambda e: e[3])
    cur: list = []
    for e in allf:
        if cur and e[3] - cur[-1][3] > gap:
            groups[str(int(cur[0][3]))] = cur
            cur = []
        cur.append(e)
    if cur:
        groups[str(int(cur[0][3]))] = cur
    items = []
    for sid, entries in groups.items():
        start = entries[0][3]
        items.append(_summary(sid, time.strftime("%d %b %Y, %H:%M", time.localtime(start)), entries))
    items.sort(key=lambda s: s["start"] or 0, reverse=True)
    return items, groups


def scan(idx: int, fresh: bool = False) -> tuple[dict, list[dict], dict]:
    src = _source(idx)
    with _lock:
        c = _cache.get(idx)
        if c and not fresh and time.time() - c[0] < CACHE_TTL:
            return src, c[1], c[2]
    items, groups = _scan(src)
    notes = load_notes()
    for it in items:
        it["note"] = notes.get(f"{src['name']}/{it['id']}", "")
    with _lock:
        _cache[idx] = (time.time(), items, groups)
    return src, items, groups


def overview() -> dict:
    out = []
    for s in sources():
        _, items, _ = scan(s["idx"])
        finfo = []
        for f in s["files"]:
            try:
                st = os.stat(f)
                finfo.append({"path": f, "name": os.path.basename(f), "size": st.st_size, "mtime": st.st_mtime, "exists": True})
            except OSError:
                finfo.append({"path": f, "name": os.path.basename(f), "exists": False})
        out.append({**s, "folders": [{"path": f, "exists": os.path.isdir(f)} for f in s["folders"]], "files": finfo,
                    "items": items, "total_size": sum(i["size"] for i in items),
                    "total_count": sum(i["count"] for i in items)})
    du = shutil.disk_usage(files.ROOTS[0])
    return {"sources": out, "disk": {"total": du.total, "free": du.free, "used": du.used}}


def _entries(idx: int, item_id: str) -> tuple[dict, list]:
    src, _, groups = scan(idx, fresh=True)
    if item_id not in groups:
        raise HTTPException(404, "run/session not found (it may have been deleted)")
    return src, groups[item_id]


def list_files(idx: int, item_id: str, kind: str = "") -> list[dict]:
    _, entries = _entries(idx, item_id)
    out = []
    for folder, p, size, mt in entries:
        if size < 0:
            continue
        if kind == "images" and not IMAGE_RE.search(p):
            continue
        out.append({"path": p, "folder": os.path.basename(folder), "name": os.path.basename(p), "size": size, "mtime": mt})
    out.sort(key=lambda f: (f["mtime"], f["name"]))
    return out


def zip_of(idx: int, item_id: str):
    src, entries = _entries(idx, item_id)
    _, summaries, _ = scan(idx)
    newest = next((x["id"] for x in summaries if x["count"]), None)
    items = []
    if item_id == newest:  # result files are overwritten each run -> they belong to the latest run
        for f in src["files"]:
            if os.path.isfile(f):
                files.safe(f)
                items.append((f, os.path.join("results", os.path.basename(f))))
    for folder, p, size, mt in entries:
        if size < 0:
            continue
        files.safe(p)  # sandbox check
        items.append((p, os.path.relpath(p, os.path.dirname(folder))))
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{src['name']}-{item_id}")
    return name, files.zip_items(iter(items))


def delete(idx: int, item_id: str) -> int:
    src, entries = _entries(idx, item_id)
    n = 0
    if src["mode"] == "runs":
        for folder, p, size, mt in entries:
            if size < 0:  # the run folder itself
                d = files.safe(p)
                if d in files.ROOTS or os.path.dirname(d) != folder:
                    raise HTTPException(400, f"refusing to delete {d}")
                n += sum(1 for _ in _files_in(d, True))
                shutil.rmtree(d)
    else:
        for folder, p, size, mt in entries:
            files.safe(p)
            os.unlink(p)
            n += 1
    with _lock:
        _cache.pop(idx, None)
    return n


def load_notes() -> dict:
    try:
        with open(NOTES_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set_note(idx: int, item_id: str, note: str) -> None:
    src = _source(idx)
    notes = load_notes()
    key = f"{src['name']}/{item_id}"
    if note.strip():
        notes[key] = note.strip()[:200]
    else:
        notes.pop(key, None)
    with open(NOTES_PATH + ".tmp", "w") as f:
        json.dump(notes, f, indent=1)
    os.replace(NOTES_PATH + ".tmp", NOTES_PATH)
    with _lock:
        _cache.pop(idx, None)


# ---------------------------------------------------------------- thumbnails
THUMB_DIR = os.path.join(CONFIG["state_dir"], "thumbs")
_thumb_sem = threading.Semaphore(2)  # at most 2 decodes at once
THUMB_CACHE_MAX = 300 * 1024 * 1024
_made = 0


def _prune_thumbs() -> None:
    allf = []
    for d, _, fs in os.walk(THUMB_DIR):
        for f in fs:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
                allf.append((st.st_atime, st.st_size, p))
            except OSError:
                pass
    total = sum(a[1] for a in allf)
    for _, size, p in sorted(allf):
        if total <= THUMB_CACHE_MAX * 0.8:
            break
        try:
            os.unlink(p)
            total -= size
        except OSError:
            pass


def thumbnail(path: str, width: int = 320) -> str:
    """Path of a cached JPEG thumbnail (generated on first request)."""
    import hashlib

    from PIL import Image, ImageOps

    p = files.safe(path)
    st = os.stat(p)
    width = max(64, min(int(width), 640))
    key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{width}".encode()).hexdigest()
    out = os.path.join(THUMB_DIR, key[:2], key + ".jpg")
    if os.path.exists(out):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with _thumb_sem:
        with Image.open(p) as im:
            im.draft("RGB", (width * 2, width * 2))  # JPEG: decode at reduced size (fast)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((width, width * 2))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            tmp = out + ".tmp"
            im.save(tmp, "JPEG", quality=75)
            os.replace(tmp, out)
    global _made
    _made += 1
    if _made % 200 == 0:
        _prune_thumbs()
    return out
