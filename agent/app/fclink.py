"""The panel's MAVLink link to the flight controller.

mavlink-router enabled: a background thread listens on the panel's local
UDP port (udpin:127.0.0.1:<clients.panel>) and keeps the latest heartbeat,
GPS, RC and servo-output messages; commands go out the same way. The FC
serial port is never touched by the panel.

mavlink-router disabled: nothing runs in the background. For a command or
a telemetry snapshot the panel opens the FC port briefly, and only if no
other program has it open.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import HTTPException

from . import drone, hardware

os.environ.setdefault("MAVLINK20", "1")  # the FC speaks MAVLink 2

MSG_SYS_STATUS, MSG_GPS_RAW_INT, MSG_SERVO_OUTPUT_RAW, MSG_RC_CHANNELS = 1, 24, 36, 65
CMD_SET_MESSAGE_INTERVAL, CMD_REQUEST_MESSAGE = 511, 512
RC_SENSOR_BIT = 1 << 16  # MAV_SYS_STATUS_SENSOR_RC_RECEIVER
ACK_NAMES = {0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED", 4: "FAILED", 5: "IN_PROGRESS"}
STALE = 5.0
SRC = (255, 191)      # the panel's MAVLink identity (GCS system id, onboard-computer component)
SNIFFER_SYSID = 253   # mavlink-router SnifferSysid: the endpoint that has this id receives ALL traffic


class Telemetry:
    def __init__(self) -> None:
        self.hb = self.gps = self.rc = self.sys = None
        self.servos: dict[int, int] = {}
        self.t_hb = self.t_gps = self.t_rc = self.t_sys = self.t_servo = 0.0
        self.acks: dict[int, tuple[float, int]] = {}
        self.target = (1, 1)
        # link quality: MAVLink sequence numbers of the autopilot's packets (gaps = lost/corrupted packets)
        self.rx_n = self.rx_lost = 0
        self._last_seq: int | None = None
        self.hist: deque = deque()  # (time, rx_n, rx_lost), one entry per second

    def _count(self, msg, now: float) -> None:
        if msg.get_srcSystem() != self.target[0] or msg.get_srcComponent() != 1:
            return
        seq = msg.get_seq()
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) % 256
            self.rx_lost += gap if gap < 128 else 0  # a huge jump is an FC reboot / reorder, not loss
        self._last_seq = seq
        self.rx_n += 1
        if not self.hist or now - self.hist[-1][0] >= 1.0:
            self.hist.append((now, self.rx_n, self.rx_lost))
            while self.hist and now - self.hist[0][0] > 60:
                self.hist.popleft()

    def link_stats(self, window: float = 10.0) -> dict | None:
        """Messages per second and % lost from the autopilot over the last `window` seconds."""
        now = time.time()
        base = next((h for h in self.hist if now - h[0] <= window), None)
        if base is None or now - base[0] < 1.0:
            return None
        dn, dl, dt = self.rx_n - base[1], self.rx_lost - base[2], now - base[0]
        return {"msgs_per_s": round(dn / dt, 1), "loss_pct": round(100 * dl / max(1, dn + dl), 2),
                "lost": dl, "window_s": round(dt, 1)}

    def handle(self, msg) -> None:
        now = time.time()
        self._count(msg, now)
        t = msg.get_type()
        if t == "HEARTBEAT":
            if msg.get_srcComponent() != 1 or msg.type in (6, 18):  # ignore GCS / companion heartbeats
                return
            self.hb, self.t_hb, self.target = msg, now, (msg.get_srcSystem(), msg.get_srcComponent())
        elif t == "GPS_RAW_INT":
            self.gps, self.t_gps = msg, now
        elif t == "RC_CHANNELS":
            self.rc, self.t_rc = msg, now
        elif t == "SYS_STATUS":
            self.sys, self.t_sys = msg, now
        elif t == "SERVO_OUTPUT_RAW":
            base = 1 + 8 * getattr(msg, "port", 0)
            for i in range(1, 17):
                v = getattr(msg, f"servo{i}_raw", None)
                if v is not None:
                    self.servos[base + i - 1] = v
            self.t_servo = now
        elif t == "COMMAND_ACK":
            # as the router's sniffer we also see ACKs meant for other programs: only take ours
            ts, tc = getattr(msg, "target_system", 0), getattr(msg, "target_component", 0)
            if ts in (0, SRC[0]) and tc in (0, SRC[1]):
                self.acks[msg.command] = (now, msg.result)

    def snapshot(self) -> dict:
        now = time.time()
        fresh = lambda t: bool(t) and now - t < STALE
        out = {"connected": fresh(self.t_hb), "age": round(now - self.t_hb, 1) if self.t_hb else None}
        if self.hb is not None and fresh(self.t_hb):
            out["armed"] = bool(self.hb.base_mode & 128)
            out["autopilot"] = self.hb.autopilot
        if self.gps is not None and fresh(self.t_gps):
            g = self.gps
            out["gps"] = {"fix": g.fix_type, "sats": g.satellites_visible if g.satellites_visible != 255 else None,
                          "hdop": round(g.eph / 100, 2) if g.eph not in (0, 65535) else None}
        if self.rc is not None and fresh(self.t_rc):
            rssi = self.rc.rssi
            out["rc"] = {"channels": self.rc.chancount, "quality": round(rssi / 254 * 100) if rssi != 255 else None}
        if self.sys is not None and fresh(self.t_sys):
            s = self.sys
            out["rc_sensor"] = {"present": bool(s.onboard_control_sensors_present & RC_SENSOR_BIT),
                                "enabled": bool(s.onboard_control_sensors_enabled & RC_SENSOR_BIT),
                                "healthy": bool(s.onboard_control_sensors_health & RC_SENSOR_BIT)}
        ls = self.link_stats()
        if ls and fresh(self.t_hb):
            out["link"] = ls
        if self.t_servo:
            out["servos"] = {str(k): v for k, v in sorted(self.servos.items())}
            out["servos_age"] = round(now - self.t_servo, 1)
        return out


def _open(url: str, baud: int, **kw):
    from pymavlink import mavutil
    # udp_timeout: forget router addresses that went silent (a restarted mavlink-router talks from a
    # new port; pymavlink otherwise keeps writing to the dead one too and drops our packets)
    return mavutil.mavlink_connection(url, baud=baud, source_system=SRC[0], source_component=SRC[1],
                                      autoreconnect=False, udp_timeout=2, **kw)


class RouterLink:
    """Persistent listener on the panel's mavlink-router UDP port."""

    def __init__(self) -> None:
        self.tel = Telemetry()
        self.conn = None
        self.url = None
        self.send_lock = threading.Lock()
        self.last_request = 0.0
        self.last_register = 0.0
        self.reg_addr = None
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="fclink")
        self._thread.start()

    def _run(self) -> None:
        want, checked = None, 0.0
        while True:
            if time.time() - checked > 1.0:  # re-read drone.yaml once a second, not per message
                checked = time.time()
                try:
                    d = drone.load()
                    want = drone.client_url(d, "panel")[0] if d["mavlink_router"]["enabled"] else None
                except Exception as e:  # drone.yaml unreadable: stay idle
                    want, self.error = None, str(e)
            if want != self.url:
                self._close()
                self.url = want
                if want:
                    try:
                        self.conn = _open(want, 115200)
                        self.error = ""
                    except Exception as e:
                        self.error = f"cannot listen on {want}: {e}"
                        self.conn = None
            if not self.conn:
                time.sleep(2)
                continue
            try:
                msg = self.conn.recv_match(blocking=True, timeout=1)
            except Exception as e:
                self.error = str(e)
                time.sleep(1)
                continue
            if msg is not None:
                self.tel.handle(msg)
            self._register_sniffer()
            self._keep_streams()

    def _register_sniffer(self) -> None:
        """Tell mavlink-router this endpoint also has system id 253 (its SnifferSysid), so the panel
        sees every packet from the FC (needed for an honest packet-loss figure). The message is
        addressed to system 253 itself, which nobody else has, so the router forwards it nowhere."""
        now = time.time()
        addr = getattr(self.conn, "last_address", None)
        # once per router (re)start, i.e. when the router's address changes, and every 60 s as a fallback
        if addr == self.reg_addr and now - self.last_register < 60:
            return
        self.reg_addr, self.last_register = addr, now
        with self.send_lock:
            mav = self.conn.mav
            old = (mav.srcSystem, mav.srcComponent)
            mav.srcSystem, mav.srcComponent = SNIFFER_SYSID, SRC[1]
            try:
                mav.command_long_send(SNIFFER_SYSID, SRC[1], CMD_REQUEST_MESSAGE, 0, 0, 0, 0, 0, 0, 0, 0)
            finally:
                mav.srcSystem, mav.srcComponent = old

    def _close(self) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _keep_streams(self) -> None:
        """Ask the FC for the few messages we need if they're not arriving (at most every 10 s)."""
        now = time.time()
        if not self.tel.t_hb or now - self.tel.t_hb > STALE or now - self.last_request < 10:
            return
        need = [(MSG_GPS_RAW_INT, self.tel.t_gps, 1_000_000), (MSG_RC_CHANNELS, self.tel.t_rc, 500_000),
                (MSG_SYS_STATUS, self.tel.t_sys, 1_000_000), (MSG_SERVO_OUTPUT_RAW, self.tel.t_servo, 1_000_000)]
        for msg_id, t, interval in need:
            if not t or now - t > STALE:
                self.command(CMD_SET_MESSAGE_INTERVAL, [msg_id, interval], wait=False)
        self.last_request = now

    def command(self, cmd: int, params: list[float], wait: bool = True, timeout: float = 3.0) -> str:
        if not self.conn:
            raise HTTPException(503, "mavlink-router link not ready")
        if not self.tel.t_hb or time.time() - self.tel.t_hb > STALE:
            raise HTTPException(504, "No heartbeat from the flight controller through mavlink-router")
        p = (list(params) + [0] * 7)[:7]
        sent = time.time()
        with self.send_lock:
            self.conn.mav.command_long_send(self.tel.target[0], self.tel.target[1], cmd, 0, *[float(x) for x in p])
        if not wait:
            return "SENT"
        while time.time() - sent < timeout:
            a = self.tel.acks.get(cmd)
            if a and a[0] >= sent:
                return ACK_NAMES.get(a[1], str(a[1]))
            time.sleep(0.01)
        raise HTTPException(504, "Flight controller did not acknowledge the command (no COMMAND_ACK)")


router_link: RouterLink | None = None


def start() -> None:
    global router_link
    if router_link is None:
        router_link = RouterLink()


# ---------------------------------------------------------------- direct (no router)
_direct_lock = threading.Lock()


def _direct_session(d: dict, fn):
    port = d["fc"]["port"]
    real = os.path.realpath(port)
    if not os.path.exists(real):
        raise HTTPException(503, f"{port} not found: is the flight controller connected?")
    owners, _ = hardware.device_owners({real})
    own = owners.get(real, [])
    if own:
        names = ", ".join(sorted({(o["unit"] or o["comm"]).replace(".service", "") for o in own}))
        raise HTTPException(409, f"{port} is in use by {names}. Without mavlink-router only one program can use "
                                 f"the FC port; enable mavlink-router in drone.yaml to share it.")
    if not _direct_lock.acquire(blocking=False):
        raise HTTPException(409, "Another FC request is in progress")
    m = None
    try:
        m = _open(port, d["fc"]["baud"])
        hb = m.wait_heartbeat(timeout=5)
        if hb is None:
            raise HTTPException(504, f"No heartbeat from the flight controller on {port} at {d['fc']['baud']} baud")
        tel = Telemetry()
        tel.handle(hb)
        return fn(m, tel)
    finally:
        if m:
            try:
                m.close()
            except Exception:
                pass
        _direct_lock.release()


def _collect(m, tel: Telemetry, wanted: set[str], secs: float) -> None:
    end = time.time() + secs
    while time.time() < end:
        msg = m.recv_match(blocking=True, timeout=0.3)
        if msg is not None:
            tel.handle(msg)
            if all(getattr(tel, f"t_{w}") for w in wanted):
                return


# ---------------------------------------------------------------- public API (blocking; call via to_thread)
def _wait_first_heartbeat(secs: float = 15.0) -> None:
    """Right after the link (re)starts nothing has arrived yet: give the FC a moment."""
    end = time.time() + secs
    while time.time() < end and (not router_link.tel.t_hb or time.time() - router_link.tel.t_hb > STALE):
        time.sleep(0.2)


def mode() -> str:
    return "router" if drone.load()["mavlink_router"]["enabled"] else "direct"


def snapshot(fresh_direct: bool = True) -> dict:
    """Latest FC telemetry: live from the router link, or a short direct read."""
    d = drone.load()
    if d["mavlink_router"]["enabled"]:
        start()
        _wait_first_heartbeat()
        snap = router_link.tel.snapshot()
        return {"mode": "router", "error": router_link.error, **snap}
    if not fresh_direct:
        return {"mode": "direct", "connected": None}

    def read(m, tel):
        for msg_id in (MSG_GPS_RAW_INT, MSG_RC_CHANNELS, MSG_SYS_STATUS):
            m.mav.command_long_send(m.target_system, m.target_component, CMD_REQUEST_MESSAGE, 0, msg_id, 0, 0, 0, 0, 0, 0)
        _collect(m, tel, {"gps", "rc", "sys"}, 4)
        return tel.snapshot()
    return {"mode": "direct", **_direct_session(d, read)}


def set_servo(servo: int, pwm: int) -> dict:
    """DO_SET_SERVO with acknowledgement, plus the servo output read back."""
    d = drone.load()
    t0 = time.time()
    if d["mavlink_router"]["enabled"]:
        start()
        _wait_first_heartbeat()
        ack = router_link.command(183, [servo, pwm])
        if ack != "ACCEPTED":
            raise HTTPException(502, f"Flight controller rejected the servo command: {ack}")
        t_ack = time.time()  # outputs reported before the ACK may still show the old value
        router_link.command(CMD_REQUEST_MESSAGE, [MSG_SERVO_OUTPUT_RAW], wait=False)
        end = time.time() + 1.5
        while time.time() < end and router_link.tel.t_servo <= t_ack:
            time.sleep(0.05)
        snap = router_link.tel.snapshot()
        return {"ack": ack, "readback": (snap.get("servos") or {}).get(str(servo)), "outputs": snap.get("servos"),
                "fc": {"armed": snap.get("armed")}, "ms": round((time.time() - t0) * 1000), "via": "router"}

    def do(m, tel):
        m.mav.command_long_send(m.target_system, m.target_component, 183, 0, float(servo), float(pwm), 0, 0, 0, 0, 0)
        end = time.time() + 3
        while time.time() < end and 183 not in tel.acks:
            msg = m.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.3)
            if msg is not None:
                tel.handle(msg)
        if 183 not in tel.acks:
            raise HTTPException(504, "Flight controller did not acknowledge the servo command (no COMMAND_ACK)")
        ack = ACK_NAMES.get(tel.acks[183][1], str(tel.acks[183][1]))
        if ack != "ACCEPTED":
            raise HTTPException(502, f"Flight controller rejected the servo command: {ack}")
        m.mav.command_long_send(m.target_system, m.target_component, CMD_REQUEST_MESSAGE, 0, MSG_SERVO_OUTPUT_RAW, 0, 0, 0, 0, 0, 0)
        _collect(m, tel, {"servo"}, 2)
        snap = tel.snapshot()
        return {"ack": ack, "readback": (snap.get("servos") or {}).get(str(servo)), "outputs": snap.get("servos"),
                "fc": {"armed": snap.get("armed")}, "ms": round((time.time() - t0) * 1000), "via": "direct"}
    return _direct_session(d, do)


def read_outputs() -> dict:
    d = drone.load()
    if d["mavlink_router"]["enabled"]:
        start()
        router_link.command(CMD_REQUEST_MESSAGE, [MSG_SERVO_OUTPUT_RAW], wait=False)
        time.sleep(1.0)
        snap = router_link.tel.snapshot()
        return {"fc": {"armed": snap.get("armed")}, "outputs": snap.get("servos") or {}, "via": "router"}

    def do(m, tel):
        m.mav.command_long_send(m.target_system, m.target_component, CMD_REQUEST_MESSAGE, 0, MSG_SERVO_OUTPUT_RAW, 0, 0, 0, 0, 0, 0)
        _collect(m, tel, {"servo"}, 2)
        snap = tel.snapshot()
        return {"fc": {"armed": snap.get("armed")}, "outputs": snap.get("servos") or {}, "via": "direct"}
    return _direct_session(d, do)


# ---------------------------------------------------------------- two-way link check (preflight)
def _clients_running(d: dict) -> list[dict]:
    """Which programs from drone.yaml currently have their mavlink-router UDP port open."""
    import psutil
    ports = {p: n for n, p in d["mavlink_router"]["clients"].items() if n != "panel"}
    found: dict[str, int] = {}
    try:
        for c in psutil.net_connections(kind="udp"):
            if c.laddr and c.laddr.port in ports and c.pid:
                found[ports[c.laddr.port]] = c.pid
    except (psutil.AccessDenied, OSError):
        pass
    out = []
    for name in ports.values():
        pid = found.get(name)
        out.append({"name": name, "running": pid is not None,
                    "unit": hardware._unit_of(pid) if pid else None})
    return out


def link_check(timeout: float = 2.0) -> dict:
    """FC -> OBC: heartbeat, message rate and packet loss.
    OBC -> FC: send a harmless request (MAV_CMD_REQUEST_MESSAGE SYS_STATUS) and wait for the FC's
    COMMAND_ACK, which proves our packets reach the FC. Changes nothing on the FC."""
    d = drone.load()
    if d["mavlink_router"]["enabled"]:
        start()
        _wait_first_heartbeat()
        tel = router_link.tel
        snap = tel.snapshot()
        out = {"mode": "router", "clients": _clients_running(d),
               "rx": {"ok": bool(snap.get("connected")), "age": snap.get("age"), **(tel.link_stats() or {})}}
        if router_link.error:
            out["rx"]["error"] = router_link.error
        if not out["rx"]["ok"]:
            out["tx"] = {"ok": None, "error": "not tested (nothing received from the FC)"}
            return out
        t0 = time.time()
        try:
            ack = router_link.command(CMD_REQUEST_MESSAGE, [MSG_SYS_STATUS], timeout=timeout)
            t_ack = tel.acks.get(CMD_REQUEST_MESSAGE, (time.time(), 0))[0]
            out["tx"] = {"ok": True, "ack": ack, "rtt_ms": max(1, round((t_ack - t0) * 1000))}
        except HTTPException as e:
            out["tx"] = {"ok": False, "error": str(e.detail)}
        return out

    def do(m, tel):
        # FC -> OBC: listen ~3 s (asking for the preflight messages at the same time)
        for msg_id in (MSG_GPS_RAW_INT, MSG_RC_CHANNELS, MSG_SYS_STATUS):
            m.mav.command_long_send(m.target_system, m.target_component, CMD_REQUEST_MESSAGE, 0, msg_id, 0, 0, 0, 0, 0, 0)
        end = time.time() + 3
        while time.time() < end:
            msg = m.recv_match(blocking=True, timeout=0.2)
            if msg is not None:
                tel.handle(msg)
        res = {"mode": "direct", "rx": {"ok": True, "age": 0.0, **(tel.link_stats(window=5) or {})}}
        # OBC -> FC
        tel.acks.pop(CMD_REQUEST_MESSAGE, None)
        t0 = time.time()
        m.mav.command_long_send(m.target_system, m.target_component, CMD_REQUEST_MESSAGE, 0, MSG_SYS_STATUS, 0, 0, 0, 0, 0, 0)
        while time.time() - t0 < timeout and CMD_REQUEST_MESSAGE not in tel.acks:
            msg = m.recv_match(blocking=True, timeout=0.1)
            if msg is not None:
                tel.handle(msg)
        a = tel.acks.get(CMD_REQUEST_MESSAGE)
        res["tx"] = ({"ok": True, "ack": ACK_NAMES.get(a[1], str(a[1])), "rtt_ms": max(1, round((a[0] - t0) * 1000))} if a
                     else {"ok": False, "error": "no COMMAND_ACK from the flight controller"})
        res["snapshot"] = tel.snapshot()
        return res
    try:
        return _direct_session(d, do)
    except HTTPException as e:
        if e.status_code == 504:  # opened the port but no heartbeat: FC -> OBC is broken
            return {"mode": "direct", "rx": {"ok": False, "error": str(e.detail)},
                    "tx": {"ok": None, "error": "not tested (nothing received from the FC)"}}
        raise
