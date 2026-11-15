"""Photos and video recorded on the Jetson (not the camera's SD card).

Folder: drone.yaml `capture.folder` (default ~/camera_captures), with photos/
and videos/ inside.

* Photo: one full-resolution frame from the camera's RTSP stream, decoded on
  the Jetson (hardware decoder + JPEG encoder when available), saved as JPEG.
* Video: the camera's own H.265/H.264 stream written to an MP4 file without
  re-encoding (almost no CPU). The MP4 is fragmented (written in 1 s pieces),
  so even if recording is cut off (power loss, panel restart) the file up to
  that point plays.

Each photo / recording opens its own RTSP connection to the camera.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import time
from collections import deque

from fastapi import HTTPException

from . import camera, drone

BOUNDARY = "dpcapture"
PHOTO_TIMEOUT = 15.0
PHOTO_SKIP = 2          # decoded frames to skip before keeping one (first frames after connect can be soft)


def folder() -> str:
    return os.path.expanduser(drone.load()["capture"]["folder"])


def _subdir(kind: str) -> str:
    d = os.path.join(folder(), kind)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, f"Cannot create {d}: {e.strerror}")
    return d


def _free_gb(path: str) -> float:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return 0.0


async def _codec(cam: camera.CameraStream) -> str:
    if cam.cam["url"] == "test":
        return "test"
    c = cam.codec or cam.cam.get("codec", "auto")
    if c not in ("h264", "h265"):
        c = await camera.probe_codec(cam.cam["url"])
        if c is None:
            raise HTTPException(503, f"Camera not reachable at {cam.cam['url']}")
        cam.codec = c
    return c


def _src(url: str, codec: str) -> str:
    depay = "rtph265depay ! h265parse" if codec == "h265" else "rtph264depay ! h264parse"
    return f"rtspsrc location={shlex.quote(url)} latency=200 protocols=tcp ! {depay}"


# ---------------------------------------------------------------- photo
_photo_lock = asyncio.Lock()


async def photo(idx: int) -> dict:
    cam = camera.get(idx)
    if _photo_lock.locked():
        raise HTTPException(409, "Already taking a photo")
    async with _photo_lock:
        codec = await _codec(cam)
        out_dir = _subdir("photos")
        name = time.strftime("photo_%Y%m%d_%H%M%S.jpg")
        path = os.path.join(out_dir, name)
        sink = f"multipartmux boundary={BOUNDARY} ! fdsink fd=1 sync=false"
        if codec == "test":
            pipe = f"videotestsrc num-buffers=10 pattern=smpte ! video/x-raw,width=1920,height=1080 ! jpegenc quality=95 ! {sink}"
        elif cam.hw:
            pipe = f"{_src(cam.cam['url'], codec)} ! nvv4l2decoder ! nvvidconv ! video/x-raw,format=I420 ! nvjpegenc quality=95 ! {sink}"
        else:
            avdec = "avdec_h265" if codec == "h265" else "avdec_h264"
            pipe = f"{_src(cam.cam['url'], codec)} ! {avdec} ! videoconvert ! jpegenc quality=95 ! {sink}"
        proc = await asyncio.create_subprocess_exec(*shlex.split(f"gst-launch-1.0 -q {pipe}"),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    limit=64 << 20)
        frame = None
        try:
            frame = await asyncio.wait_for(_nth_jpeg(proc.stdout, PHOTO_SKIP + 1), PHOTO_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        finally:
            if proc.returncode is None:
                proc.kill()
            err = (await proc.stderr.read()).decode(errors="replace").strip()[-300:]
            await proc.wait()
        if not frame:
            raise HTTPException(504, "No picture from the camera" + (f": {err}" if err else f" within {PHOTO_TIMEOUT:g} s"))
        with open(path, "wb") as f:
            f.write(frame)
        return {"ok": True, "path": path, "name": name, "size": len(frame)}


async def _nth_jpeg(out: asyncio.StreamReader, n: int) -> bytes | None:
    """Read multipart JPEG frames from gst stdout; return the n-th (or the last one if it ends early)."""
    last, count = None, 0
    while True:
        line = await out.readline()
        if not line:
            return last
        if BOUNDARY.encode() not in line:
            continue
        length = 0
        while True:
            h = await out.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            if h.lower().startswith(b"content-length:"):
                length = int(h.split(b":", 1)[1].strip())
        if not length:
            continue
        last = await out.readexactly(length)
        count += 1
        if count >= n:
            return last


# ---------------------------------------------------------------- video
class Recording:
    def __init__(self, idx: int, proc, path: str, user: str) -> None:
        self.idx, self.proc, self.path, self.user = idx, proc, path, user
        self.started = time.time()
        self.stderr: deque = deque(maxlen=20)
        self.stop_reason = ""

    def info(self) -> dict:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {"camera": self.idx, "path": self.path, "name": os.path.basename(self.path), "user": self.user,
                "started": self.started, "seconds": round(time.time() - self.started), "size": size}


recordings: dict[int, Recording] = {}
last_stopped: dict[int, dict] = {}


async def start_recording(idx: int, user: str) -> dict:
    cam = camera.get(idx)
    if idx in recordings:
        raise HTTPException(409, "Already recording this camera")
    d = drone.load()["capture"]
    out_dir = _subdir("videos")
    free = _free_gb(out_dir)
    if free < d["min_free_gb"]:
        raise HTTPException(507, f"Only {free:.1f} GB free on the Jetson (minimum {d['min_free_gb']:g} GB, capture.min_free_gb in drone.yaml)")
    codec = await _codec(cam)
    name = time.strftime("video_%Y%m%d_%H%M%S.mp4")
    path = os.path.join(out_dir, name)
    mux = f"mp4mux fragment-duration=1000 ! filesink location={shlex.quote(path)}"
    if codec == "test":
        enc = "x264enc tune=zerolatency" if camera._has("x264enc") else "openh264enc"   # development only
        pipe = f"videotestsrc is-live=true ! video/x-raw,width=1280,height=720,framerate=25/1 ! {enc} ! h264parse ! {mux}"
    else:
        pipe = f"{_src(cam.cam['url'], codec)} ! queue ! {mux}"
    proc = await asyncio.create_subprocess_exec(*shlex.split(f"gst-launch-1.0 -e -q {pipe}"),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    rec = Recording(idx, proc, path, user)
    recordings[idx] = rec
    asyncio.create_task(_watch(rec))
    await asyncio.sleep(2.5)  # fail fast if the camera refuses the connection
    if proc.returncode is not None:
        recordings.pop(idx, None)
        raise HTTPException(502, "Recording failed to start: " + (" ".join(rec.stderr)[-300:] or "camera stream not available"))
    return rec.info()


async def _watch(rec: Recording) -> None:
    async def read_err():
        while True:
            line = await rec.proc.stderr.readline()
            if not line:
                return
            rec.stderr.append(line.decode(errors="replace").strip())
    t = asyncio.create_task(read_err())
    min_free = drone.load()["capture"]["min_free_gb"]
    while rec.proc.returncode is None:
        try:
            await asyncio.wait_for(rec.proc.wait(), 5)
        except asyncio.TimeoutError:
            if _free_gb(os.path.dirname(rec.path)) < min_free / 2:
                rec.stop_reason = f"stopped automatically: less than {min_free / 2:g} GB free on the Jetson"
                await _stop_proc(rec)
    await t
    if recordings.get(rec.idx) is rec:  # ended on its own (camera lost, disk full, ...)
        recordings.pop(rec.idx, None)
        rec.stop_reason = rec.stop_reason or ("recording stopped: " + (" ".join(rec.stderr)[-200:] or "camera stream ended"))
    last_stopped[rec.idx] = {**rec.info(), "reason": rec.stop_reason, "ended": time.time()}


async def _stop_proc(rec: Recording) -> None:
    if rec.proc.returncode is None:
        rec.proc.send_signal(signal.SIGINT)  # gst-launch -e: end-of-stream, so the MP4 is closed properly
        try:
            await asyncio.wait_for(rec.proc.wait(), 8)
        except asyncio.TimeoutError:
            rec.proc.kill()
            await rec.proc.wait()


async def stop_recording(idx: int) -> dict:
    rec = recordings.pop(idx, None)
    if not rec:
        raise HTTPException(409, "Not recording")
    rec.stop_reason = "stopped"
    await _stop_proc(rec)
    return {**rec.info(), "reason": "stopped"}


async def stop_all() -> None:
    for idx in list(recordings):
        try:
            await stop_recording(idx)
        except HTTPException:
            pass


# ---------------------------------------------------------------- status
def recent(limit: int = 12) -> list[dict]:
    out = []
    for kind in ("photos", "videos"):
        d = os.path.join(folder(), kind)
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_file() and not e.name.startswith("."):
                        st = e.stat()
                        out.append({"path": e.path, "name": e.name, "kind": kind[:-1], "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    rec_paths = {r.path for r in recordings.values()}
    return [{**x, "recording": x["path"] in rec_paths} for x in out[:limit]]


def status() -> dict:
    f = folder()
    return {"folder": f, "free_gb": round(_free_gb(f if os.path.isdir(f) else os.path.expanduser("~")), 1),
            "min_free_gb": drone.load()["capture"]["min_free_gb"],
            "recording": {str(k): r.info() for k, r in recordings.items()},
            "last_stopped": {str(k): v for k, v in last_stopped.items()},
            "recent": recent()}
