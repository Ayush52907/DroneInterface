"""On-demand camera preview (MJPEG over HTTP).

Nothing runs until someone opens a preview. Then one GStreamer process per
camera decodes the RTSP stream (hardware decoder + hardware JPEG encoder on
Jetson), downscales and rate-limits it, and the latest JPEG is fanned out to
all viewers. The process stops a few seconds after the last viewer leaves,
and every viewer connection is closed after `camera_preview.max_minutes`.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import time
from collections import deque

from .config import CONFIG

BOUNDARY = b"dpframe"
IDLE_STOP_SEC = 4
QUALITY = {  # name -> (width, fps, jpeg quality)
    "low": (480, 5, 55),      # ~0.6 Mbit/s (measured on SIYI A8 mini)
    "medium": (640, 10, 60),  # ~2 Mbit/s
    "high": (1280, 15, 70),   # ~6-8 Mbit/s
}


async def probe_codec(url: str, timeout: float = 4.0) -> str | None:
    """Ask the RTSP server (DESCRIBE) which codec it sends: 'h264' / 'h265'."""
    import re
    from urllib.parse import urlparse
    u = urlparse(url)
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 554), timeout)
        w.write(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: drone-panel\r\n\r\n".encode())
        await w.drain()
        data = b""
        while b"m=video" not in data or b"rtpmap" not in data.split(b"m=video", 1)[-1]:
            chunk = await asyncio.wait_for(r.read(4096), timeout)
            if not chunk:
                break
            data += chunk
        w.close()
    except (OSError, asyncio.TimeoutError):
        return None
    m = re.search(rb"a=rtpmap:\d+ (H26[45]|HEVC)/", data, re.I)
    if not m:
        return None
    return "h264" if m.group(1).upper() == b"H264" else "h265"


def _has(element: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    return subprocess.run(["gst-inspect-1.0", element], capture_output=True).returncode == 0


class CameraStream:
    def __init__(self, idx: int, cam: dict) -> None:
        self.idx = idx
        self.cam = cam
        self.proc: asyncio.subprocess.Process | None = None
        self.codec: str | None = None
        self.frame: bytes | None = None
        self.seq = 0
        self.cond = asyncio.Condition()
        self.viewers = 0
        self.quality = "medium"
        self.started_at = 0.0
        self.error = ""
        self.stderr: deque = deque(maxlen=20)
        self.frame_times: deque = deque(maxlen=200)
        self.bytes_window: deque = deque(maxlen=200)
        self._stop_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ pipeline
    def pipeline(self) -> list[str]:
        w, fps, q = QUALITY.get(self.quality, QUALITY["medium"])
        h = int(w * 9 / 16) // 2 * 2
        url = self.cam["url"]
        sink = f"multipartmux boundary={BOUNDARY.decode()} ! fdsink fd=1 sync=false"
        if url == "test":  # development: synthetic picture
            src = f"videotestsrc is-live=true pattern=ball ! video/x-raw,width={w},height={h},framerate={fps}/1 ! clockoverlay"
            enc = f"jpegenc quality={q}"
            return shlex.split(f"gst-launch-1.0 -q {src} ! {enc} ! {sink}")
        codec = self.codec or "h264"
        depay = "rtph265depay ! h265parse" if codec == "h265" else "rtph264depay ! h264parse"
        src = f"rtspsrc location={shlex.quote(url)} latency=100 protocols=tcp drop-on-latency=true ! {depay}"
        if self.hw:
            dec = (f"nvv4l2decoder ! nvvidconv ! video/x-raw,format=I420,width={w},height={h} "
                   f"! videorate drop-only=true max-rate={fps} ! nvjpegenc quality={q}")
        else:
            avdec = "avdec_h265" if codec == "h265" else "avdec_h264"
            dec = (f"{avdec} ! videoconvert ! videoscale ! video/x-raw,width={w},height={h} "
                   f"! videorate drop-only=true max-rate={fps} ! jpegenc quality={q}")
        return shlex.split(f"gst-launch-1.0 -q {src} ! {dec} ! {sink}")

    @property
    def hw(self) -> bool:
        if not hasattr(self, "_hw"):
            self._hw = _has("nvv4l2decoder") and _has("nvjpegenc")
        return self._hw

    async def _start(self) -> None:
        self.codec = self.cam.get("codec", "auto")
        if self.cam["url"] != "test" and self.codec not in ("h264", "h265"):
            self.codec = await probe_codec(self.cam["url"])
            if self.codec is None:
                self.error = f"Camera not reachable at {self.cam['url']}"
                self.proc = None
                return
        self.error = ""
        self.frame = None
        self.stderr.clear()
        self.frame_times.clear()
        self.bytes_window.clear()
        self.proc = await asyncio.create_subprocess_exec(
            *self.pipeline(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 << 20)
        self.started_at = time.time()
        asyncio.create_task(self._read(self.proc))
        asyncio.create_task(self._read_err(self.proc))

    async def _read(self, proc) -> None:
        out = proc.stdout
        try:
            while True:
                line = await out.readline()
                if not line:
                    break
                if BOUNDARY not in line:
                    continue
                length = 0
                while True:  # part headers
                    h = await out.readline()
                    if not h or h in (b"\r\n", b"\n"):
                        break
                    if h.lower().startswith(b"content-length:"):
                        length = int(h.split(b":", 1)[1].strip())
                if not length:
                    continue
                data = await out.readexactly(length)
                now = time.time()
                self.frame_times.append(now)
                self.bytes_window.append((now, length))
                async with self.cond:
                    self.frame = data
                    self.seq += 1
                    self.cond.notify_all()
        except (asyncio.IncompleteReadError, ValueError):
            pass
        rc = await proc.wait()
        if proc is self.proc:
            self.proc = None
            if rc not in (0, -15, -9):
                self.error = self._explain()
        async with self.cond:
            self.cond.notify_all()

    async def _read_err(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            self.stderr.append(line.decode(errors="replace").strip())

    def _explain(self) -> str:
        txt = " ".join(self.stderr)
        if "Could not open resource" in txt or "Could not connect" in txt or "timeout" in txt.lower():
            return f"Camera not reachable at {self.cam['url']}"
        if "Unauthorized" in txt or "401" in txt:
            return "Camera refused the connection (authentication)"
        if "not-negotiated" in txt or "no element" in txt.lower():
            return "Video pipeline error (codec mismatch?): " + txt[-200:]
        return "Preview stopped: " + (txt[-200:] or "camera stream ended")

    async def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                p.kill()
        self.frame = None
        async with self.cond:
            self.cond.notify_all()

    async def ensure(self, quality: str | None) -> None:
        async with self._lock:
            if self._stop_task:
                self._stop_task.cancel()
                self._stop_task = None
            if quality and quality in QUALITY and quality != self.quality:
                self.quality = quality
                await self.stop()
            if self.proc is None or self.proc.returncode is not None:
                await self._start()

    def _schedule_idle_stop(self) -> None:
        async def later():
            await asyncio.sleep(IDLE_STOP_SEC)
            if self.viewers == 0:
                await self.stop()
        if self._stop_task:
            self._stop_task.cancel()
        self._stop_task = asyncio.create_task(later())

    # ------------------------------------------------------------ viewers
    async def frames(self, max_seconds: float):
        """Async generator of multipart chunks for one viewer."""
        self.viewers += 1
        deadline = time.monotonic() + max_seconds
        last = -1
        try:
            while time.monotonic() < deadline:
                async with self.cond:
                    try:
                        await asyncio.wait_for(self.cond.wait_for(lambda: self.seq != last or self.proc is None), 5)
                    except asyncio.TimeoutError:
                        if self.proc is None:
                            break
                        continue
                    if self.seq == last and self.proc is None:
                        break
                    data, last = self.frame, self.seq
                if data:
                    yield (b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
        finally:
            self.viewers -= 1
            if self.viewers <= 0:
                self.viewers = 0
                self._schedule_idle_stop()

    def status(self) -> dict:
        now = time.time()
        recent = [t for t in self.frame_times if t > now - 5]
        kb = sum(n for t, n in self.bytes_window if t > now - 5) / 5 / 1024
        w, fps, q = QUALITY[self.quality]
        return {
            "idx": self.idx, "name": self.cam.get("name", f"Camera {self.idx + 1}"), "url": self.cam["url"],
            "running": self.proc is not None and self.proc.returncode is None,
            "viewers": self.viewers, "quality": self.quality,
            "width": w, "height": int(w * 9 / 16) // 2 * 2, "target_fps": fps,
            "fps": round(len(recent) / 5, 1), "kbps": round(kb * 8), "since": self.started_at if self.proc else None,
            "error": self.error, "hw": self.hw if self.cam["url"] != "test" else False,
            "codec": self.codec,
        }


streams: list[CameraStream] = [CameraStream(i, c) for i, c in enumerate(CONFIG["cameras"])]


def get(idx: int) -> CameraStream:
    if not 0 <= idx < len(streams):
        raise ValueError("unknown camera")
    return streams[idx]


async def stop_all() -> None:
    for s in streams:
        await s.stop()
