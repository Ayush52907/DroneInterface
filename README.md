# Drone Panel

**A secure web control panel for the companion computer (NVIDIA Jetson) on a drone.**

Drone Panel replaces day-to-day SSH work on the drone's onboard computer with a browser UI that works
from any laptop or phone on your Tailscale network. It shows the system's health, runs the mission
services, checks the aircraft before flight, and talks to the flight controller and camera. The
onboard agent is lightweight and resource-capped, so it never competes with the flight and vision
software.

- **One place for the whole onboard stack:** services, logs, files, terminal, ROS 2, camera, gimbal, payload and flight data.
- **Preflight GO / NO GO:** an editable checklist covering services, hardware links, the camera, the FC ↔ OBC link (both directions), RC and GPS.
- **Flight controller sharing:** optional [mavlink-router](https://github.com/mavlink-router/mavlink-router) integration lets several programs use the FC at once.
- **Private by design:** reachable only inside your tailnet over HTTPS, with every change recorded.
- **Light:** hard-capped at 25% of one core and 450 MB RAM, with no build step and no cloud dependency.

---

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Flight controller and mavlink-router](#flight-controller-and-mavlink-router)
- [Security model](#security-model)
- [Resource usage](#resource-usage)
- [Repository layout](#repository-layout)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

---

## Features

### System
| Page | What it does |
|---|---|
| **Dashboard** | Live CPU, GPU, RAM, temperatures, power, disk, network and Tailscale status. |
| **Services** | Start, stop, restart, enable and disable systemd services, with their recent logs. Includes a unit-file editor (validated with `systemd-analyze verify`, previous versions backed up) and a list of the services you choose to show. |
| **Logs** | Live journald logs from one or more services, with filters, search and download. |
| **Files** | Browse, view, edit, live-tail, upload (resumable), download (folders as zip), copy, move, rename and delete, inside configured roots only. Image viewer with arrow-key navigation. |
| **Terminal** | Browser shell that survives connection drops. |
| **Hardware** | Ethernet, Wi-Fi and CAN status, USB tree, serial ports and **which process holds each port**, cameras, I²C scan, network device reachability (ping, TCP, RTSP), and plug/unplug history. |
| **ROS 2** | Nodes, topics, publishers → subscribers, live rate and bandwidth, latest message, and "no publisher" warnings. The probe runs only while the page is in use. |
| **Activity** | Audit log of every change: who did what, when, and whether it worked. |

### Flight operations
| Page | What it does |
|---|---|
| **Preflight** | GO / NO GO checklist with critical and warning checks, auto-refresh, and one-click "add to preflight" from other pages. Built-in checks include:<ul><li>services, devices, serial port owners, USB devices, network links, ping, TCP, RTSP</li><li>ROS 2 nodes and topic rates</li><li>disk space, temperature, Tailscale, gimbal position</li><li>**FC ↔ OBC link in both directions:** heartbeat, packet loss, and a request the FC must answer</li><li>RC link (ELRS), GPS fix, satellites, HDOP</li></ul> |
| **Quick actions** | One-button sequences of service, gimbal and command steps, e.g. "start mission pipeline". |
| **Camera** | <ul><li>**Preview:** on-demand live preview with hardware decoding on the Jetson. It's off until started and stops by itself after a set time or when the tab is hidden.</li><li>**Photo / Record:** saved **on the Jetson**. Videos are written without re-encoding and still play if recording is cut off.</li><li>**Gimbal (SIYI):** a verified "Flight position" button, plus manual pan, tilt and zoom. In manual control the panel actively holds the position, correcting gyro drift.</li></ul> |
| **Payload** | Payload-release servo control through the flight controller (MAVLink `DO_SET_SERVO`) with acknowledgement and output read-back. Configurable positions; release positions ask for confirmation. |
| **Flight data** | Pipeline output grouped into numbered runs or time-based sessions: image galleries, notes, zip download, cleanup. |
| **Map** | Geotagged detections on satellite imagery, with the bounding-box and raw images per target. Targets can be ignored or restored; this writes back to the targets file. Updates live, with one-click export to Google Earth (KML/KMZ). |
| **Drone settings** | Edits `~/drone.yaml`, the single file for the drone's important settings (see [Configuration](#configuration)). It validates the file, applies it, and shows mavlink-router status, live FC link quality, and which programs are connected. |
| **Alerts** | Shown in the panel and as browser notifications: camera lost, USB-TTL problems, serial port conflicts, gimbal not in flight position. |

---

## Architecture

```
 Laptop / phone (browser)
        │  HTTPS, inside the tailnet only
        ▼
 tailscale serve  ──►  Drone Panel agent (FastAPI, 127.0.0.1:8080, runs as the normal user)
                          │
                          ├── /proc, /sys, journald, systemd ───────────────── system state
                          ├── dpctl (via sudoers) ────────────────────────────── the only privileged path
                          ├── GStreamer (on demand) ──── RTSP ───────────────── camera
                          ├── SIYI SDK over UDP ─────────────────────────────── gimbal
                          └── MAVLink ── mavlink-router ── serial ─────────── flight controller
                                              ├── mission scripts (one UDP port each)
                                              └── optional ground stations
```

- **Backend:** Python 3.10+ with FastAPI and uvicorn, running as a systemd service with CPU, memory and IO limits.
- **Frontend:** Preact + htm with all assets vendored, so there's no build step and no CDN; the UI works offline on the tailnet.
- **State:** the panel config is in `/etc/drone-panel/config.yaml`, drone settings in `~/drone.yaml`, and runtime state (checklists, quick actions, alerts, audit log) in `~/.local/state/drone-panel/`.

---

## Requirements

| | |
|---|---|
| Hardware | NVIDIA Jetson. Developed and tested on an Orin Nano (JetPack 7, Ubuntu 24.04, Python 3.12). |
| Python | 3.10 or newer, with `venv` |
| Network | [Tailscale](https://tailscale.com) on the Jetson and on every viewing device, with **HTTPS Certificates** enabled in the tailnet's DNS settings |
| Access | SSH key access from the laptop to the Jetson, and `sudo` on the Jetson for installation |
| Optional | GStreamer (camera preview and capture), ROS 2 (ROS page), mavlink-router (FC sharing), pymavlink-compatible flight controller (ArduPilot tested) |

---

## Installation

From the laptop, once:

```bash
ssh-copy-id <user>@<jetson>          # e.g. orin@orin (Tailscale MagicDNS name)
```

Install or update:

```bash
./deploy.sh <user>@<jetson>
```

`deploy.sh` copies `agent/` to the Jetson and runs `install.sh`, which:

1. creates a virtual environment in `/opt/drone-panel` and installs the dependencies,
2. installs the privileged helper `dpctl` and its sudoers rule,
3. installs and starts the `drone-panel` systemd service with resource limits,
4. publishes the panel with `tailscale serve` and prints its address, e.g. `https://<jetson>.<tailnet>.ts.net`.

Re-running the installer is safe. It keeps the existing configuration and state.

---

## Configuration

There are two files, each with its own purpose:

| File | Holds | Edit with |
|---|---|---|
| `/etc/drone-panel/config.yaml` | How the **panel** behaves: file roots, cameras, network targets, ROS setup, flight data sources, alerts, gimbal connection | Any editor, then `sudo systemctl restart drone-panel` |
| `~/drone.yaml` | The **drone's** important settings: FC serial port and baud, mavlink-router on/off and its clients, payload servo positions, preflight limits (RC, GPS, link loss), photo/video folder | **Drone settings** page → Save & apply, or any editor |

Saving `drone.yaml` from the panel validates it first and regenerates everything derived from it:
the mavlink-router config and one environment file per program in `~/drone-env/`. If applying fails,
the previous file is restored, so `drone.yaml` always matches what is running.

---

## Flight controller and mavlink-router

Without a router, only one program can open the FC serial port at a time. With
`mavlink_router.enabled: true` in `drone.yaml`, mavlink-router owns the port and every program gets
its own local UDP port:

```yaml
mavlink_router:
  enabled: true
  clients:            # program name: local UDP port
    panel: 14551
    drop_scout: 14550
    drop_drop: 14552
    spare: 14553
```

Programs started as services pick up their connection from `~/drone-env/<name>.env`
(`EnvironmentFile=`). Their code then reads `MAVLINK_PORT`, which is `udpin:127.0.0.1:<port>` with the
router on and the serial device with it off, so switching modes needs no code change.

Guidelines for MAVLink scripts that use the router:

- **Never open the serial port directly** while the router is on.
- **Wait for the autopilot's own heartbeat.** Other programs' heartbeats also arrive through the router.
- **Use a unique `source_component` per program.** The FC's replies then reach only the program that asked.
- **Pass `udp_timeout=2` to `mavlink_connection()`.** Otherwise pymavlink keeps writing to a restarted router's old address, and commands can be lost.
- **Remember message rates are shared.** `SET_MESSAGE_INTERVAL` affects every program, so never set a stream to 0 or −1.
- **System ID 253 is reserved** for the panel's link-quality monitor (`SnifferSysid`).

---

## Security model

- **Network:** the agent listens on `127.0.0.1` only. It is published through `tailscale serve`
  (HTTPS) **inside the tailnet only**; nothing is exposed on Wi-Fi, Ethernet or the internet.
- **Identity:** Tailscale tells the agent which user is connecting. Every tailnet member has full
  access by default; restrict it with `auth.allowed_users` in `config.yaml`.
- **Least privilege:** the agent runs as the normal user, not root. Its only privileged path is
  `/opt/drone-panel/bin/dpctl` (via `/etc/sudoers.d/drone-panel`), which allows only:
  - controlling existing system units,
  - writing validated unit files and mavlink-router configs,
  - listing serial-port owners,
  - opening a shell as the normal user.
- **Files:** the file manager is confined to `files.roots` and the user's own permissions.
- **Audit:** every state-changing action is recorded with user, time and result (**Activity** page).

---

## Resource usage

The service is limited with `CPUQuota=25%`, `MemoryMax=450M`, `Nice=10` and low IO priority, so it
cannot starve flight or vision processes.

| Component | Behaviour |
|---|---|
| Stats | Sampled every 5 s with nobody watching, every 1 s while the dashboard is open |
| Camera preview / ROS 2 probe | Started on demand only; stop automatically when nobody uses them |
| Video recording | Writes the camera's stream as is: no decoding or encoding |
| mavlink-router | About 3 MB RAM and ~0% CPU; adds no measurable latency |

---

## Repository layout

```
deploy.sh                     laptop → Jetson: copy and install
agent/
  install.sh, uninstall.sh    installer (idempotent) and remover
  drone-panel.service         systemd unit with resource limits
  config.yaml                 default panel config → /etc/drone-panel/config.yaml
  preflight.default.json      initial preflight checklist
  actions.default.json        initial quick actions
  requirements.txt
  bin/dpctl                   the only privileged helper
  app/                        backend (FastAPI)
    main.py                   HTTP routes and websockets
    auth.py, audit.py         Tailscale identity, audit log
    stats.py                  /proc and /sys collector
    services.py, logs.py      systemd and journald
    files.py, terminal.py     sandboxed files, PTY sessions
    hardware.py               interfaces, USB, serial owners, I²C, reachability, events
    preflight.py              checklist engine
    ros.py, ros_helper.py     on-demand ROS 2 probe
    camera.py, capture.py     preview (GStreamer); photo and video capture
    gimbal.py                 SIYI gimbal protocol, flight position, manual hold
    drone.py                  drone.yaml: validation, mavlink-router config, env files
    fclink.py                 MAVLink link to the FC (router or direct), link quality
    payload.py                payload servos via DO_SET_SERVO
    flightdata.py, mapdata.py flight data runs, geotag map
    alerts.py, actions.py     alerts, quick actions
  web/                        frontend (Preact + htm, vendored, no build step)
```

---

## Development

Run the backend locally with authentication off:

```bash
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt
cd agent && DRONE_PANEL_AUTH=none ../.venv/bin/uvicorn app.main:app --port 8099 --reload
# open http://127.0.0.1:8099
```

A camera with `url: test` in the config produces a synthetic picture for working on the Camera page
without hardware.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Not authorized" | Open the `https://….ts.net` address, not `127.0.0.1`. Tagged devices have no user identity; use a user-owned device. |
| HTTPS address doesn't work | Enable **HTTPS Certificates** in the Tailscale admin console (DNS), then re-run the installer. |
| Start / stop fails | Check that `/etc/sudoers.d/drone-panel` exists, and re-run the installer. |
| Logs page empty | The user must be in the `systemd-journal` group (the installer adds it). Restart the service. |
| ROS 2 page empty | Check that `ros.setup` points at your `setup.bash` files and that `ros.domain_id` matches. |
| Preflight "FC ↔ OBC link" fails | The message names the broken direction. Check the FC TX → Jetson RX wire (nothing received) or the Jetson TX → FC RX wire (FC doesn't answer), plus port and baud in `drone.yaml`. |
| Script receives FC data but its commands are ignored | With mavlink-router: missing `udp_timeout=2`. Restart the script and add it. |
| Panel down | `journalctl -u drone-panel -n 50`, or `sudo systemctl restart drone-panel` over SSH. |

---

## Uninstall

```bash
sudo ~/drone-panel-src/uninstall.sh            # keep the configuration
sudo ~/drone-panel-src/uninstall.sh --purge    # also delete the configuration
```
