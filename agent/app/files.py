"""Sandboxed file operations. Every path is resolved (symlinks included) and
must stay inside one of files.roots."""
from __future__ import annotations

import asyncio
import codecs
import io
import mimetypes
import os
import queue
import shutil
import stat as statmod
import threading
import zipfile

from fastapi import HTTPException

from .config import CONFIG

ROOTS: list[str] = CONFIG["files"]["roots"]
PART_SUFFIX = ".dpart"


def _inside(p: str) -> bool:
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in ROOTS)


def safe(path: str | None, must_exist: bool = True) -> str:
    """Resolve an existing path and check it is inside the sandbox."""
    raw = os.path.expanduser(path) if path else ROOTS[0]
    p = os.path.realpath(raw)
    if not _inside(p):
        raise HTTPException(403, f"Path outside allowed folders: {path}")
    if must_exist and not os.path.lexists(p):
        raise HTTPException(404, f"Not found: {path}")
    return p


def safe_new(parent: str, name: str) -> str:
    """Path for a new entry `name` inside existing dir `parent`."""
    if not name or "/" in name or name in (".", "..") or "\0" in name:
        raise HTTPException(400, f"Invalid name: {name!r}")
    d = safe(parent)
    if not os.path.isdir(d):
        raise HTTPException(400, "Destination is not a folder")
    return os.path.join(d, name)


def safe_target(path: str) -> str:
    """Path that may not exist yet (parent must exist and be inside sandbox)."""
    path = os.path.expanduser(path)
    return safe_new(os.path.dirname(path.rstrip("/")) or "/", os.path.basename(path.rstrip("/")))


def entry(full: str, name: str | None = None) -> dict:
    name = name or os.path.basename(full)
    try:
        st = os.lstat(full)
    except OSError:
        return {"name": name, "type": "unknown"}
    is_link = statmod.S_ISLNK(st.st_mode)
    target_ok = True
    if is_link:
        try:
            st2 = os.stat(full)
            st = st2
            target_ok = _inside(os.path.realpath(full))
        except OSError:
            target_ok = False
    t = "dir" if statmod.S_ISDIR(st.st_mode) else "file" if statmod.S_ISREG(st.st_mode) else "other"
    return {
        "name": name,
        "type": t,
        "size": st.st_size if t == "file" else None,
        "mtime": st.st_mtime,
        "mode": statmod.filemode(st.st_mode),
        "link": is_link,
        "link_ok": target_ok,
        "exec": bool(st.st_mode & 0o111) and t == "file",
    }


def list_dir(path: str | None) -> dict:
    p = safe(path)
    if not os.path.isdir(p):
        raise HTTPException(400, "Not a folder")
    try:
        names = os.listdir(p)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    items = [entry(os.path.join(p, n), n) for n in names if not n.endswith(PART_SUFFIX)]
    items.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    root = max((r for r in ROOTS if p == r or p.startswith(r.rstrip("/") + "/")), key=len)
    try:
        du = shutil.disk_usage(p)
        disk = {"total": du.total, "free": du.free}
    except OSError:
        disk = None
    return {"path": p, "root": root, "parent": None if p == root else os.path.dirname(p),
            "entries": items, "disk": disk}


def kind_of(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime:
        for k in ("image", "video", "audio"):
            if mime.startswith(k + "/"):
                return k
        if mime == "application/pdf":
            return "pdf"
    return "text"


def read_text(path: str) -> dict:
    p = safe(path)
    if not os.path.isfile(p):
        raise HTTPException(400, "Not a file")
    size = os.path.getsize(p)
    limit = CONFIG["files"]["max_text_view_bytes"]
    with open(p, "rb") as f:
        head = f.read(min(size, limit))
    binary = b"\0" in head[:8192]
    return {
        "path": p, "size": size, "mtime": os.path.getmtime(p),
        "binary": binary, "truncated": size > limit,
        "content": "" if binary else head.decode("utf-8", errors="replace"),
        "kind": kind_of(p),
    }


def write_text(path: str, content: str, expected_mtime: float | None) -> dict:
    exists = os.path.exists(os.path.realpath(os.path.expanduser(path)))
    p = safe(path) if exists else safe_target(path)
    if exists and expected_mtime is not None and abs(os.path.getmtime(p) - expected_mtime) > 1e-3:
        raise HTTPException(409, "File changed on disk since you opened it. Reload before saving.")
    mode = os.stat(p).st_mode & 0o7777 if exists else 0o644
    tmp = p + ".dpanel-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, p)
    return entry(p)


def mkdir(parent: str, name: str) -> dict:
    p = safe_new(parent, name)
    os.mkdir(p)
    return entry(p)


def rename(path: str, new_name: str) -> dict:
    src = safe(path)
    if src in ROOTS:
        raise HTTPException(400, "Cannot rename a root folder")
    dst = safe_new(os.path.dirname(src), new_name)
    if os.path.lexists(dst):
        raise HTTPException(409, f"{new_name} already exists")
    os.rename(src, dst)
    return entry(dst)


def _unique(dst: str) -> str:
    base, ext = os.path.splitext(dst)
    i = 1
    cand = f"{base} copy{ext}"
    while os.path.lexists(cand):
        i += 1
        cand = f"{base} copy {i}{ext}"
    return cand


def transfer(paths: list[str], dest_dir: str, mode: str, overwrite: bool) -> list[str]:
    if mode not in ("copy", "move"):
        raise HTTPException(400, "mode must be copy or move")
    d = safe(dest_dir)
    if not os.path.isdir(d):
        raise HTTPException(400, "Destination is not a folder")
    srcs = [safe(p) for p in paths]
    plan = []
    conflicts = []
    for s in srcs:
        if s in ROOTS:
            raise HTTPException(400, "Cannot move/copy a root folder")
        if os.path.isdir(s) and (d == s or d.startswith(s + "/")):
            raise HTTPException(400, f"Cannot {mode} a folder into itself: {os.path.basename(s)}")
        dst = os.path.join(d, os.path.basename(s))
        if os.path.realpath(os.path.dirname(s)) == d:
            if mode == "move":
                continue
            dst = _unique(dst)
        elif os.path.lexists(dst):
            if not overwrite:
                conflicts.append(os.path.basename(s))
            plan.append((s, dst, True))
            continue
        plan.append((s, dst, False))
    if conflicts:
        raise HTTPException(409, {"conflicts": conflicts})
    done = []
    for s, dst, replace in plan:
        if replace:
            _remove(dst)
        if mode == "copy":
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.copytree(s, dst, symlinks=True)
            else:
                shutil.copy2(s, dst, follow_symlinks=False)
        else:
            shutil.move(s, dst)
        done.append(dst)
    return done


def _remove(p: str) -> None:
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    else:
        os.unlink(p)


def delete(paths: list[str]) -> int:
    targets = []
    for path in paths:
        # do not follow a final symlink: delete the link itself
        raw = os.path.abspath(os.path.expanduser(path))
        if os.path.realpath(raw) in ROOTS and not os.path.islink(raw):
            raise HTTPException(400, "Cannot delete a root folder")
        parent = safe(os.path.dirname(raw))
        p = os.path.join(parent, os.path.basename(raw))
        if not os.path.lexists(p):
            raise HTTPException(404, f"Not found: {path}")
        if os.path.realpath(p) in ROOTS and not os.path.islink(p):
            raise HTTPException(400, "Cannot delete a root folder")
        targets.append(p)
    for p in targets:
        _remove(p)
    return len(targets)


# ---------------------------------------------------------------- uploads
def part_path(dest: str) -> str:
    return os.path.join(os.path.dirname(dest), "." + os.path.basename(dest) + PART_SUFFIX)


def upload_status(path: str) -> dict:
    dest = safe_target(path)
    part = part_path(dest)
    return {"exists": os.path.lexists(dest),
            "partial": os.path.getsize(part) if os.path.exists(part) else 0}


# ---------------------------------------------------------------- zip download
class _QueueWriter(io.RawIOBase):
    def __init__(self, q: queue.Queue, cancel: threading.Event):
        self.q, self.cancel = q, cancel

    def writable(self):
        return True

    def write(self, b):
        data = bytes(b)
        while True:
            if self.cancel.is_set():
                raise OSError("download cancelled")
            try:
                self.q.put(data, timeout=0.5)
                return len(data)
            except queue.Full:
                continue


def _walk_items(root: str):
    """(full path | None for empty dir, arcname) for everything under root."""
    base = os.path.dirname(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, base)
        if not filenames and not dirnames:
            yield None, rel + "/"
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if not fn.endswith(PART_SUFFIX) and os.path.isfile(full):
                yield full, os.path.join(rel, fn)


def zip_stream(path: str):
    return zip_items(_walk_items(safe(path)))


def zip_items(items):
    """Stream a ZIP (stored, no compression: cheap on CPU) of (full, arcname) pairs."""
    q: queue.Queue = queue.Queue(maxsize=64)
    cancel = threading.Event()
    _END = object()

    def worker():
        try:
            w = io.BufferedWriter(_QueueWriter(q, cancel), buffer_size=256 * 1024)
            with zipfile.ZipFile(w, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for full, arc in items:
                    if full is None:
                        zf.writestr(arc, b"")
                        continue
                    try:
                        zf.write(full, arc)
                    except OSError:
                        if cancel.is_set():
                            raise
            w.flush()
        except Exception:
            pass
        finally:
            while not cancel.is_set():
                try:
                    q.put(_END, timeout=0.5)
                    break
                except queue.Full:
                    continue

    async def gen():
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            while True:
                chunk = await asyncio.to_thread(q.get)
                if chunk is _END:
                    break
                yield chunk
        finally:
            cancel.set()

    return gen()


# ---------------------------------------------------------------- live tail
async def tail(ws, path: str, initial_bytes: int = 64 * 1024) -> None:
    import json

    p = safe(path)
    if not os.path.isfile(p):
        await ws.send_text(json.dumps({"type": "error", "error": "Not a file"}))
        return

    async def watch_client():
        try:
            while True:
                await ws.receive_text()
        except Exception:
            return

    ctask = asyncio.create_task(watch_client())
    f = open(p, "rb")
    try:
        ino = os.fstat(f.fileno()).st_ino
        size = os.fstat(f.fileno()).st_size
        start = max(0, size - initial_bytes)
        f.seek(start)
        data = f.read()
        if start > 0 and b"\n" in data:
            data = data[data.index(b"\n") + 1:]
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        await ws.send_text(json.dumps({"type": "data", "reset": True, "data": dec.decode(data)}))
        while not ctask.done():
            await asyncio.sleep(0.5)
            try:
                st = os.stat(p)
            except FileNotFoundError:
                continue
            if st.st_ino != ino:  # rotated / replaced
                f.close()
                f = open(p, "rb")
                ino = st.st_ino
                dec.reset()
                await ws.send_text(json.dumps({"type": "data", "reset": True,
                                               "note": "file was replaced", "data": dec.decode(f.read(initial_bytes))}))
                continue
            if st.st_size < f.tell():  # truncated
                f.seek(0)
                dec.reset()
                await ws.send_text(json.dumps({"type": "data", "reset": True, "note": "file was truncated", "data": ""}))
            chunk = f.read(512 * 1024)
            if chunk:
                await ws.send_text(json.dumps({"type": "data", "data": dec.decode(chunk)}))
    finally:
        f.close()
        ctask.cancel()
