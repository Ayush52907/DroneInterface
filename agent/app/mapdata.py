"""Targets on a map: parses result files such as targets_clustered.txt.

Format (comment lines carry metadata for the data line that follows):
    # person 5: conf 0.528, 2026-11-10 14:46:40.904, frame 14_46_40_904, 120 px from centre, 19 detections, ...
    5, 13.0346489, 77.5672834
Each target's photo is found by its frame name in the source's run folders
(target_images first), e.g. target_images/7/14_46_40_904.jpg.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from xml.sax.saxutils import escape

from fastapi import HTTPException

from . import files, flightdata

IMG_EXT = (".jpg", ".jpeg", ".png")
_own_writes: dict[str, float] = {}  # path -> mtime of the panel's last edit
IGNORE_PREFIX = "# IGNORED "  # a comment for drop_drop.py (it skips '#' lines), recognisable for us
META_RE = {
    "conf": re.compile(r"\bconf ([\d.]+)"),
    "time": re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)"),
    "frame": re.compile(r"\bframe ([\w.-]+)"),
    "detections": re.compile(r"(\d+) detections"),
    "max_conf": re.compile(r"max conf ([\d.]+)"),
    "centre_px": re.compile(r"(\d+) px from centre"),
}


def result_files() -> list[dict]:
    """Result files from the flight data sources that contain coordinates."""
    out = []
    for s in flightdata.sources():
        for f in s["files"]:
            if not os.path.isfile(f):
                continue
            try:
                n = len(parse(f)["targets"])
            except Exception:
                n = 0
            if n:
                out.append({"path": f, "name": os.path.basename(f), "source": s["idx"], "source_name": s["name"],
                            "mtime": os.path.getmtime(f), "count": n})
    out.sort(key=lambda f: ("clustered" not in f["name"], f["name"]))
    return out


def parse(path: str) -> dict:
    p = files.safe(path)
    text = open(p, errors="replace").read(2_000_000)
    targets, notes, header = [], [], None
    pending_meta: dict | None = None
    pending_label = ""
    for line in text.splitlines():
        t = line.strip()
        if not t:
            continue
        ignored = t.startswith(IGNORE_PREFIX.strip())
        if ignored:
            t = t[len(IGNORE_PREFIX.strip()):].strip()
        if t.startswith("#"):
            body = t.lstrip("#").strip()
            m = re.match(r"(\w+) (\w+):\s*(.*)", body)
            if m and any(r.search(m.group(3)) for r in META_RE.values()):
                pending_label = f"{m.group(1)} {m.group(2)}"
                pending_meta = {k: (r.search(m.group(3)).group(1) if r.search(m.group(3)) else None) for k, r in META_RE.items()}
            elif header is None and "lat" in body.lower() and "," in body:
                header = [h.strip() for h in re.split(r"\(|\s{2,}", re.sub(r"(?i)^data lines:\s*", "", body))[0].split(",") if h.strip()]
            else:
                notes.append(body)
            continue
        cols = [c.strip() for c in t.split(",")]
        li, lo = 1, 2
        if header:
            low = [h.lower() for h in header]
            li = next((i for i, h in enumerate(low) if h.startswith("lat")), 1)
            lo = next((i for i, h in enumerate(low) if h.startswith("lon")), 2)
        try:
            lat, lon = float(cols[li]), float(cols[lo])
        except (IndexError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        tgt = {"id": cols[0], "lat": lat, "lon": lon, "label": pending_label or f"#{cols[0]}", "ignored": ignored}
        if header:
            tgt["fields"] = {header[i]: c for i, c in enumerate(cols) if i < len(header) and i not in (li, lo)}
        if pending_meta:
            tgt.update({k: v for k, v in pending_meta.items() if v is not None})
        targets.append(tgt)
        pending_meta, pending_label = None, ""
    mt = os.path.getmtime(p)
    return {"path": p, "name": os.path.basename(p), "mtime": mt, "own_write": abs(_own_writes.get(p, -1) - mt) < 1e-6, "targets": targets,
            "notes": notes[:20]}


def stat(path: str) -> dict:
    """Cheap change check for live updates: no file read, just metadata."""
    p = files.safe(path)
    st = os.stat(p)
    return {"mtime": st.st_mtime, "size": st.st_size, "own_write": abs(_own_writes.get(p, -1) - st.st_mtime) < 1e-6}


def set_ignored(path: str, tid: str, lat: float, lon: float, ignored: bool, mtime: float | None) -> dict:
    """Comment out (ignore) or restore one target's data line, atomically."""
    p = files.safe(path)
    if mtime is not None and abs(os.path.getmtime(p) - mtime) > 1e-3:
        raise HTTPException(409, "The targets file changed since you loaded it (pipeline still writing?). Reload the map and try again.")
    with open(p, errors="replace") as f:
        lines = f.read().split("\n")
    hit = None
    for i, line in enumerate(lines):
        t = line.strip()
        is_ign = t.startswith(IGNORE_PREFIX.strip())
        body = t[len(IGNORE_PREFIX.strip()):].strip() if is_ign else t
        if not body or body.startswith("#"):
            continue
        cols = [c.strip() for c in body.split(",")]
        try:
            if cols[0] == str(tid) and abs(float(cols[1]) - lat) < 1e-9 and abs(float(cols[2]) - lon) < 1e-9:
                hit = (i, is_ign, body)
                break
        except (IndexError, ValueError):
            continue
    if hit is None:
        raise HTTPException(404, f"target {tid} not found in {os.path.basename(p)} (file may have been rewritten)")
    i, is_ign, body = hit
    if ignored != is_ign:
        lines[i] = (IGNORE_PREFIX + body) if ignored else body
        mode = os.stat(p).st_mode & 0o7777
        tmp = p + ".dpanel-tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines))
        os.chmod(tmp, mode)
        os.replace(tmp, p)
        _own_writes[p] = os.path.getmtime(p)
    return with_images(p)


def _image_dirs(path: str) -> list[str]:
    """Run folders to search for target photos, best first (target_images, then
    detections, then raw frames), newest run first."""
    src = next((s for s in flightdata.sources() if path in s["files"]), None)
    if not src:
        return []
    rank = lambda f: 0 if "target" in os.path.basename(f) else 1 if "det" in os.path.basename(f) else 2
    dirs = []
    for folder in sorted(src["folders"], key=rank):
        try:
            subs = [e for e in os.scandir(folder) if e.is_dir() and not e.name.startswith(".")]
        except OSError:
            continue
        subs.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        dirs.extend([e.path for e in subs] + [folder])
    return dirs


_box_cache: dict = {}


def _box(annotated: str, raw: str):
    """Where the bounding box/label was drawn: the region where the annotated
    frame differs from the raw one. Normalised [x0, y0, x1, y1] or None."""
    from PIL import Image, ImageChops
    key = (annotated, raw, os.path.getmtime(annotated), os.path.getmtime(raw))
    if key in _box_cache:
        return _box_cache[key]
    box = None
    try:
        with Image.open(annotated) as a, Image.open(raw) as b:
            if a.size == b.size:
                w, h = a.size
                small = (w // 2, h // 2)  # half size: fast and precise enough
                diff = ImageChops.difference(a.convert("L").resize(small), b.convert("L").resize(small))
                bb = diff.point(lambda v: 255 if v > 60 else 0).getbbox()
                if bb:
                    box = [round(bb[0] / small[0], 4), round(bb[1] / small[1], 4), round(bb[2] / small[0], 4), round(bb[3] / small[1], 4)]
    except Exception:
        box = None
    _box_cache[key] = box
    return box


def _raw_dirs(path: str) -> list[str]:
    """Folders holding raw (un-annotated) frames: names containing rtsp/raw."""
    src = next((s for s in flightdata.sources() if path in s["files"]), None)
    return [f for f in (src["folders"] if src else []) if re.search(r"rtsp|raw", os.path.basename(f), re.I)]


def with_images(path: str) -> dict:
    d = parse(path)
    dirs = _image_dirs(d["path"])
    raw_roots = _raw_dirs(d["path"])
    for t in d["targets"]:
        frame = t.get("frame")
        t["image"] = None
        if not frame:
            continue
        for dd in dirs:
            for ext in IMG_EXT:
                cand = os.path.join(dd, frame + ext)
                if os.path.exists(cand):
                    t["image"] = cand
                    t["image_run"] = os.path.basename(dd)
                    break
            if t["image"]:
                break
        # raw frame: same run first, then any run
        t["raw_image"], t["box"] = None, None
        for root in raw_roots:
            cands = ([os.path.join(root, t["image_run"])] if t.get("image_run") else []) + [root]
            try:
                cands += sorted((e.path for e in os.scandir(root) if e.is_dir()), key=os.path.getmtime, reverse=True)
            except OSError:
                pass
            for dd in cands:
                for ext in IMG_EXT:
                    cand = os.path.join(dd, frame + ext)
                    if os.path.exists(cand) and cand != t["image"]:
                        t["raw_image"] = cand
                        break
                if t["raw_image"]:
                    break
            if t["raw_image"]:
                break
        if t["image"] and t["raw_image"]:
            t["box"] = _box(t["image"], t["raw_image"])
    return d


def kmz(path: str) -> tuple[str, bytes]:
    """Google Earth / Mission Planner file with a pin + photo per target."""
    from PIL import Image

    d = with_images(path)
    buf = io.BytesIO()
    pins, images = [], []
    if True:
        for t in d["targets"]:
            desc = [f"<b>{escape(t['label'])}</b><br/>Lat {t['lat']:.7f}<br/>Lon {t['lon']:.7f}"]
            for k, label in (("conf", "Confidence"), ("max_conf", "Max confidence"), ("detections", "Detections"), ("time", "Time"), ("frame", "Frame")):
                if t.get(k):
                    desc.append(f"{label}: {escape(str(t[k]))}")
            if t.get("image"):
                name = f"files/{t['id']}.jpg"
                with Image.open(t["image"]) as im:
                    im.thumbnail((800, 800))
                    b = io.BytesIO()
                    im.convert("RGB").save(b, "JPEG", quality=80)
                images.append((name, b.getvalue()))
                desc.append(f'<img src="{name}" width="400"/>')
            if t.get("ignored"):
                desc.insert(1, "<i>IGNORED (commented out in the targets file)</i>")
            pins.append(f"""<Placemark><name>{escape(str(t['id']))}{' (ignored)' if t.get('ignored') else ''}</name><styleUrl>#{'ign' if t.get('ignored') else 'tgt'}</styleUrl>
<description><![CDATA[{'<br/>'.join(desc)}]]></description>
<Point><coordinates>{t['lon']:.7f},{t['lat']:.7f},0</coordinates></Point></Placemark>""")
        kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{escape(d['name'])}</name>
<Style id="tgt"><IconStyle><color>ff00d5ff</color><scale>1.2</scale>
<Icon><href>http://maps.google.com/mapfiles/kml/paddle/red-circle.png</href></Icon></IconStyle>
<LabelStyle><scale>1.1</scale></LabelStyle></Style>
<Style id="ign"><IconStyle><color>99aaaaaa</color><scale>0.9</scale>
<Icon><href>http://maps.google.com/mapfiles/kml/paddle/wht-blank.png</href></Icon></IconStyle>
<LabelStyle><color>99aaaaaa</color></LabelStyle></Style>
{''.join(pins)}
</Document></kml>"""
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)  # Google Earth reads the first .kml in the archive
        for name, data in images:
            z.writestr(name, data)
    return os.path.splitext(d["name"])[0] + ".kmz", buf.getvalue()
