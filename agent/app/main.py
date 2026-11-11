"""Drone Panel agent: FastAPI app serving the API, WebSockets and the web UI."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import audit, camera, files, flightdata, hardware, logs, preflight, services, terminal
from .alerts import engine as alerts
from .gimbal import gimbal
from . import actions, mapdata
from .ros import bridge as ros_bridge
from .auth import require_user, ws_user
from .config import CONFIG, WEB_DIR
from .stats import collector


@asynccontextmanager
async def lifespan(app: FastAPI):
    collector.start()
    bg = [asyncio.create_task(hardware.monitor.run()), asyncio.create_task(terminal.reaper()),
          asyncio.create_task(alerts.run())]
    yield
    for t in bg:
        t.cancel()
    for sid in list(terminal.sessions):
        terminal.kill(sid)
    await ros_bridge.stop()
    await camera.stop_all()


app = FastAPI(title="Drone Panel", lifespan=lifespan, docs_url=None, redoc_url=None)
api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(PermissionError)
async def perm_handler(request, exc):
    return JSONResponse({"detail": f"Permission denied: {exc.filename or exc}"}, status_code=403)


@app.exception_handler(FileExistsError)
async def exists_handler(request, exc):
    return JSONResponse({"detail": f"Already exists: {exc.filename or exc}"}, status_code=409)


@app.exception_handler(ValueError)
async def value_handler(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ------------------------------------------------------------------ general
@api.get("/me")
def me(user: dict = Depends(require_user)):
    return {
        "user": user,
        "system": collector.info(),
        "files": {"roots": files.ROOTS, "bookmarks": CONFIG["files"]["bookmarks"]},
        "self_unit": services.SELF_UNIT,
        "features": {"terminal": CONFIG["terminal"]["enabled"], "ros": ros_bridge.status()["available"],
                     "cameras": len(CONFIG["cameras"]), "flight_data": len(CONFIG["flight_data"]["sources"])},
    }


@api.get("/stats/history")
def stats_history():
    return {"history": list(collector.history), "latest": collector.latest}


@app.websocket("/ws/stats")
async def ws_stats(ws: WebSocket):
    if not await ws_user(ws):
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    collector.subscribers.add(q)

    async def drain():
        try:
            while True:
                await ws.receive_text()
        except Exception:
            return

    dtask = asyncio.create_task(drain())
    try:
        if collector.latest:
            await ws.send_text(json.dumps(collector.latest))
        while not dtask.done():
            try:
                s = await asyncio.wait_for(q.get(), 2)
            except asyncio.TimeoutError:
                continue
            await ws.send_text(json.dumps(s))
    except Exception:
        pass
    finally:
        collector.subscribers.discard(q)
        dtask.cancel()


@api.get("/audit")
def audit_log(limit: int = 300):
    return {"entries": audit.recent(min(limit, 2000))}


# ------------------------------------------------------------------ services
@api.get("/services")
async def services_list():
    return await services.list_all()


@api.get("/services/{unit}")
async def service_detail(unit: str):
    return await services.detail(unit)


@api.get("/services/{unit}/unitfile")
async def service_unitfile(unit: str):
    return await services.unit_file(unit)


@api.post("/services/verify")
async def service_verify(name: str = Body(...), content: str = Body(...)):
    return await services.verify(name, content)


@api.put("/services/{unit}/unitfile")
async def service_unitfile_save(unit: str, user: dict = Depends(require_user), content: str = Body(...),
                                restart: bool = Body(False)):
    info = await services.unit_file(unit)
    if not info["editable"]:
        raise HTTPException(403, info["reason"])
    ok, msg = await services.write_unit(unit, content)
    audit.record(user, "service.edit", unit, ok, msg)
    if not ok:
        raise HTTPException(500, msg)
    if restart:
        ok2, msg2 = await services.action(unit, "restart")
        audit.record(user, "service.restart", unit, ok2, msg2)
        msg += "\n" + ("restarted" if ok2 else "restart failed: " + msg2)
    return {"ok": True, "message": msg}


@api.post("/services/create")
async def service_create(user: dict = Depends(require_user), name: str = Body(...), content: str = Body(...),
                         enable: bool = Body(False), start: bool = Body(False)):
    name = services.norm_unit(name)
    if await services.exists(name):
        raise HTTPException(409, f"{name} already exists")
    ok, msg = await services.write_unit(name, content)
    audit.record(user, "service.create", name, ok, msg)
    if not ok:
        raise HTTPException(500, msg)
    notes = [msg]
    for act, want in (("enable", enable), ("start", start)):
        if want:
            ok2, m2 = await services.action(name, act)
            audit.record(user, f"service.{act}", name, ok2, m2)
            notes.append(f"{act}: {'ok' if ok2 else m2}")
    return {"ok": True, "message": "\n".join(notes)}


@api.delete("/services/{unit}")
async def service_delete(unit: str, user: dict = Depends(require_user)):
    info = await services.unit_file(unit)
    if not info["editable"]:
        raise HTTPException(403, info["reason"] or "not deletable")
    ok, msg = await services.delete_unit(unit)
    audit.record(user, "service.delete", unit, ok, msg)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "message": msg}


@api.post("/services/{unit}/view")
def service_view(unit: str, mode: str = Body(..., embed=True), user: dict = Depends(require_user)):
    v = services.set_view(unit, mode)
    audit.record(user, f"service.view.{mode}", unit)
    return v


@api.post("/services/{unit}/{act}")
async def service_action(unit: str, act: str, user: dict = Depends(require_user)):
    ok, msg = await services.action(unit, act)
    audit.record(user, f"service.{act}", unit, ok, msg)
    if not ok:
        raise HTTPException(500, msg or f"systemctl {act} failed")
    info = await services.show([services.norm_unit(unit)])
    return {"ok": True, "message": msg, "info": info[0] if info else None}


# ------------------------------------------------------------------ logs
def _log_params(units: str, kernel: bool, priority: int | None, lines: int, since: str | None):
    unit_list = [u for u in units.split(",") if u] if units else []
    return unit_list, kernel, priority, lines, since or None


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket, units: str = "", kernel: bool = False, priority: int | None = None,
                  lines: int = 300, since: str = ""):
    if not await ws_user(ws):
        return
    await ws.accept()
    try:
        cmd = logs.build_cmd(*_log_params(units, kernel, priority, lines, since), follow=True, fmt="json")
    except ValueError as e:
        await ws.send_text(json.dumps({"type": "end", "error": str(e)}))
        await ws.close()
        return
    try:
        await logs.stream(ws, cmd)
    except Exception:
        pass


@api.get("/logs/download")
async def logs_download(units: str = "", kernel: bool = False, priority: int | None = None,
                        lines: int = 20000, since: str = ""):
    cmd = logs.build_cmd(*_log_params(units, kernel, priority, lines, since), follow=False, fmt="short-iso")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        try:
            while chunk := await proc.stdout.read(65536):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()

    name = (units.replace(",", "+").replace(".service", "") or ("kernel" if kernel else "journal"))
    return StreamingResponse(gen(), media_type="text/plain",
                             headers={"Content-Disposition": f'attachment; filename="{name}.log"'})


# ------------------------------------------------------------------ files
@api.get("/files/list")
def files_list(path: str = ""):
    return files.list_dir(path or None)


@api.get("/files/read")
def files_read(path: str):
    return files.read_text(path)


@api.get("/files/raw")
def files_raw(path: str):
    p = files.safe(path)
    if not os.path.isfile(p):
        raise HTTPException(400, "Not a file")
    return FileResponse(p, content_disposition_type="inline", filename=os.path.basename(p))


@api.get("/files/download")
def files_download(path: str, user: dict = Depends(require_user)):
    p = files.safe(path)
    audit.record(user, "file.download", p)
    if os.path.isdir(p):
        name = os.path.basename(p) or "files"
        return StreamingResponse(files.zip_stream(p), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
    return FileResponse(p, filename=os.path.basename(p))


@api.post("/files/write")
def files_write(user: dict = Depends(require_user), path: str = Body(...), content: str = Body(...),
                mtime: float | None = Body(None)):
    e = files.write_text(path, content, mtime)
    audit.record(user, "file.write", path, True, f"{len(content)} chars")
    return e


@api.post("/files/mkdir")
def files_mkdir(user: dict = Depends(require_user), parent: str = Body(...), name: str = Body(...)):
    e = files.mkdir(parent, name)
    audit.record(user, "file.mkdir", os.path.join(parent, name))
    return e


@api.post("/files/rename")
def files_rename(user: dict = Depends(require_user), path: str = Body(...), name: str = Body(...)):
    e = files.rename(path, name)
    audit.record(user, "file.rename", path, True, f"-> {name}")
    return e


@api.post("/files/transfer")
def files_transfer(user: dict = Depends(require_user), paths: list[str] = Body(...), dest: str = Body(...),
                   mode: str = Body(...), overwrite: bool = Body(False)):
    done = files.transfer(paths, dest, mode, overwrite)
    audit.record(user, f"file.{mode}", ", ".join(paths), True, f"-> {dest}")
    return {"done": done}


@api.post("/files/delete")
def files_delete(user: dict = Depends(require_user), paths: list[str] = Body(..., embed=True)):
    n = files.delete(paths)
    audit.record(user, "file.delete", ", ".join(paths))
    return {"deleted": n}


@api.get("/files/upload")
def upload_status(path: str):
    return files.upload_status(path)


@api.put("/files/upload")
async def upload_chunk(request: Request, path: str, offset: int = Query(..., ge=0), total: int = Query(..., ge=0),
                       overwrite: bool = False, user: dict = Depends(require_user)):
    dest = files.safe_target(path)
    if os.path.lexists(dest) and not overwrite:
        raise HTTPException(409, f"{os.path.basename(dest)} already exists")
    part = files.part_path(dest)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if offset == 0:
        have = 0
    elif offset != have:
        return JSONResponse({"detail": "offset mismatch", "partial": have}, status_code=409)
    with open(part, "wb" if offset == 0 else "ab") as f:
        async for chunk in request.stream():
            f.write(chunk)
        size = f.tell()
    if size >= total:
        os.replace(part, dest)
        audit.record(user, "file.upload", dest, True, f"{total} bytes")
        return {"done": True, "size": size}
    return {"done": False, "partial": size}


@app.websocket("/ws/tail")
async def ws_tail(ws: WebSocket, path: str):
    if not await ws_user(ws):
        return
    await ws.accept()
    try:
        await files.tail(ws, path)
    except HTTPException as e:
        await ws.send_text(json.dumps({"type": "error", "error": str(e.detail)}))
    except Exception:
        pass


# ------------------------------------------------------------------ hardware
@api.get("/hardware")
async def hardware_snapshot():
    return await hardware.snapshot()


@api.post("/hardware/ping")
async def hardware_ping(host: str = Body(..., embed=True)):
    return await hardware.ping(host)


@api.post("/hardware/i2c-scan")
async def hardware_i2c(bus: int = Body(..., embed=True), user: dict = Depends(require_user)):
    audit.record(user, "hardware.i2c_scan", f"bus {bus}")
    return await hardware.i2c_scan(bus)


# ------------------------------------------------------------------ preflight
@api.get("/preflight/checks")
def preflight_checks():
    return {"checks": preflight.load(), "types": preflight.TYPES}


@api.put("/preflight/checks")
def preflight_save(user: dict = Depends(require_user), checks: list[dict] = Body(..., embed=True)):
    saved = preflight.save(checks)
    audit.record(user, "preflight.edit", f"{len(saved)} checks")
    return {"checks": saved}


@api.post("/preflight/run")
async def preflight_run():
    return await preflight.run()


# ------------------------------------------------------------------ ROS 2
@api.get("/ros/status")
def ros_status():
    return ros_bridge.status()


async def _ros(cmd: str, args: dict | None = None, timeout: float = 15):
    try:
        return await ros_bridge.call(cmd, args, timeout)
    except Exception as e:
        raise HTTPException(503, str(e))


@api.get("/ros/graph")
async def ros_graph():
    return await _ros("graph")


@api.post("/ros/watch")
async def ros_watch(topics: list[str] = Body(..., embed=True)):
    return await _ros("set_watch", {"topics": topics})


@api.get("/ros/rates")
async def ros_rates():
    return await _ros("rates")


@api.post("/ros/echo")
async def ros_echo(topic: str = Body(..., embed=True)):
    return await _ros("echo", {"topic": topic, "timeout": 3}, timeout=10)


# ------------------------------------------------------------------ camera
@api.get("/camera")
def camera_list():
    return {"cameras": [s.status() for s in camera.streams], "max_minutes": CONFIG["camera_preview"]["max_minutes"],
            "gimbal": CONFIG["gimbal"]["enabled"],
            "qualities": camera.QUALITY}


@api.get("/camera/{idx}/stream")
async def camera_stream(idx: int, quality: str = "", user: dict = Depends(require_user)):
    s = camera.get(idx)
    was_running = s.status()["running"]
    await s.ensure(quality or None)
    if not was_running:
        audit.record(user, "camera.preview", s.cam.get("name", str(idx)), True, s.quality)
    max_s = float(CONFIG["camera_preview"]["max_minutes"]) * 60
    return StreamingResponse(s.frames(max_s), media_type=f"multipart/x-mixed-replace; boundary={camera.BOUNDARY.decode()}",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@api.get("/camera/{idx}/snapshot")
def camera_snapshot(idx: int):
    s = camera.get(idx)
    if not s.frame:
        raise HTTPException(409, "Start the preview first")
    name = time.strftime("snapshot_%Y%m%d_%H%M%S.jpg")
    return Response(s.frame, media_type="image/jpeg", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ gimbal
@api.get("/gimbal")
async def gimbal_status():
    return await gimbal.status()


@api.post("/gimbal/flight")
async def gimbal_flight(user: dict = Depends(require_user)):
    steps: list[str] = []
    r = await gimbal.flight_position(steps.append)
    audit.record(user, "gimbal.flight_position", "gimbal", r["ok"], r["detail"])
    return {**r, "steps": steps, "status": await gimbal.status(refresh=False)}


@api.post("/gimbal/move")
async def gimbal_move(yaw: int = Body(0), pitch: int = Body(0)):
    await gimbal.move(yaw, pitch)
    return {"ok": True}


@api.post("/gimbal/stop")
async def gimbal_stop():
    await gimbal.stop_move()
    return {"ok": True}


@api.post("/gimbal/cmd")
async def gimbal_cmd(action: str = Body(..., embed=True), user: dict = Depends(require_user)):
    await gimbal.simple(action)
    if action not in ("zoom_in", "zoom_out", "zoom_stop"):
        audit.record(user, f"gimbal.{action}", "gimbal")
    return {"ok": True}


# ------------------------------------------------------------------ quick actions
@api.get("/actions")
def actions_list():
    return {"actions": actions.load()}


@api.put("/actions")
def actions_save(user: dict = Depends(require_user), items: list[dict] = Body(..., embed=True)):
    saved = actions.save(items)
    audit.record(user, "actions.edit", f"{len(saved)} actions")
    return {"actions": saved}


@api.post("/actions/{aid}/run")
async def actions_run(aid: str, user: dict = Depends(require_user)):
    name = next((a["name"] for a in actions.load() if a["id"] == aid), aid)

    async def gen():
        last = ""
        async for line in actions.run(aid):
            last = line
            yield line
        audit.record(user, "action.run", name, last.startswith("DONE"), last.strip())
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


# ------------------------------------------------------------------ alerts
@api.get("/alerts")
def alerts_list():
    return alerts.full()


@api.post("/alerts/{aid}/ack")
def alerts_ack(aid: str):
    alerts.ack(aid)
    return {"ok": True}


@api.post("/alerts/mute")
def alerts_mute(minutes: float = Body(..., embed=True), user: dict = Depends(require_user)):
    alerts.mute(minutes)
    audit.record(user, "alerts.mute", f"{minutes:g} min")
    return {"ok": True}


# ------------------------------------------------------------------ map
@api.get("/map/files")
def map_files():
    return {"files": mapdata.result_files()}


@api.get("/map/stat")
def map_stat(path: str):
    return mapdata.stat(path)


@api.get("/map/targets")
def map_targets(path: str):
    return mapdata.with_images(path)


@api.post("/map/ignore")
def map_ignore(user: dict = Depends(require_user), path: str = Body(...), id: str = Body(...), lat: float = Body(...),
               lon: float = Body(...), ignored: bool = Body(...), mtime: float | None = Body(None)):
    d = mapdata.set_ignored(path, id, lat, lon, ignored, mtime)
    audit.record(user, "target.ignore" if ignored else "target.restore", f"{os.path.basename(path)} #{id}", True,
                 f"{lat:.7f}, {lon:.7f}")
    return d


@api.get("/map/kmz")
def map_kmz(path: str, user: dict = Depends(require_user)):
    name, data = mapdata.kmz(path)
    audit.record(user, "map.kmz", name)
    return Response(data, media_type="application/vnd.google-earth.kmz",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ flight data
@api.get("/data/sources")
def data_sources():
    return {**flightdata.editable_sources(), "candidates": flightdata.folder_candidates()}


@api.put("/data/sources")
def data_sources_save(user: dict = Depends(require_user), items: list[dict] = Body(..., embed=True)):
    r = flightdata.save_sources(items)
    audit.record(user, "data.sources", ", ".join(s["name"] for s in r["sources"]))
    return r


@api.post("/data/sources/reset")
def data_sources_reset(user: dict = Depends(require_user)):
    audit.record(user, "data.sources.reset", "config.yaml defaults")
    return flightdata.reset_sources()


@api.get("/data")
def data_overview():
    return flightdata.overview()


@api.get("/data/{idx}/{item_id}/files")
def data_files(idx: int, item_id: str, kind: str = ""):
    return {"files": flightdata.list_files(idx, item_id, kind)[:5000]}


@api.get("/data/{idx}/{item_id}/download")
def data_download(idx: int, item_id: str, user: dict = Depends(require_user)):
    name, gen = flightdata.zip_of(idx, item_id)
    audit.record(user, "data.download", name)
    return StreamingResponse(gen, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@api.post("/data/{idx}/{item_id}/delete")
def data_delete(idx: int, item_id: str, user: dict = Depends(require_user)):
    n = flightdata.delete(idx, item_id)
    audit.record(user, "data.delete", f"source {idx} / {item_id}", True, f"{n} files")
    return {"deleted": n}


@api.post("/data/{idx}/{item_id}/note")
def data_note(idx: int, item_id: str, note: str = Body("", embed=True)):
    flightdata.set_note(idx, item_id, note)
    return {"ok": True}


@api.get("/files/thumb")
def files_thumb(path: str, w: int = 320):
    try:
        out = flightdata.thumbnail(path, w)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(415, f"cannot make thumbnail: {e}")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ terminal
@api.get("/terminal/sessions")
def terminal_sessions():
    return {"sessions": [s.info() for s in terminal.sessions.values()], "enabled": CONFIG["terminal"]["enabled"]}


@api.delete("/terminal/sessions/{sid}")
def terminal_kill(sid: str, user: dict = Depends(require_user)):
    if terminal.kill(sid):
        audit.record(user, "terminal.close", sid)
    return {"ok": True}


@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket, session: str = "", cols: int = 80, rows: int = 24):
    user = await ws_user(ws)
    if not user:
        return
    await ws.accept()
    if not CONFIG["terminal"]["enabled"]:
        await ws.send_text(json.dumps({"t": "error", "error": "Terminal is disabled in config.yaml"}))
        await ws.close()
        return
    s = terminal.sessions.get(session)
    if s is None:
        try:
            s = terminal.create(user, cols, rows)
        except Exception as e:
            await ws.send_text(json.dumps({"t": "error", "error": str(e)}))
            await ws.close()
            return
        audit.record(user, "terminal.open", s.id)
    await terminal.attach(ws, s, cols, rows)


app.include_router(api)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
