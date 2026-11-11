# Drone Panel

A web control panel for the Jetson Orin Nano on the drone, replacing day-to-day SSH use.
Open it from any laptop or phone on your tailnet.

**Phase 1 features:** live dashboard (CPU/GPU/RAM/temps/power/network/disk/Tailscale), service
management (start/stop/restart/enable/disable, unit file, recent logs), live journald logs
(multi-service, filters, search, download), file manager (browse/view/edit/live-tail/upload with
resume/download folders as zip/copy/move/rename/delete), and an audit log of every change.

**Phase 2 features:** preflight checklist (GO / NO GO, editable, one-click "add to preflight" from
other pages), hardware & connections (Ethernet/Wi-Fi/CAN status, USB tree, serial ports and which
process uses each, cameras, I²C scan, network device reachability incl. RTSP, plug/unplug event
history), ROS 2 panel (nodes, topics, publishers → subscribers, live Hz/bandwidth, latest message,
"no publisher" warnings), and a web terminal that survives connection drops.

**Phase 3 features:** on-demand camera preview (SIYI A8 mini over RTSP; hardware decode on the
Jetson; off until you press Start; stops after 5 min, when the tab is hidden or the page is left;
~0.6/2/7 Mbit/s for low/medium/high) and a flight data manager (pipeline output grouped into numbered
runs or time-based sessions; image gallery with thumbnails; download a run as zip; notes; delete).

**Phase 4 features:** gimbal controls (Flight position button that hands control back to the
siyi-pitch service and verifies the angle; manual pan/tilt/zoom/photo/record in LOCK mode), alerts
in the panel + browser notifications (camera lost, USB-TTL problems, serial port conflict, gimbal not
in flight position), quick actions (ordered service/gimbal/command steps, e.g. start/stop the mission
pipeline), and a service editor (edit/create/delete custom unit files, checked with
`systemd-analyze verify`, previous versions backed up to /var/backups/drone-panel/units/).

## Layout

```
deploy.sh                 run on the laptop: copy agent to the Jetson and install it
agent/
  app/                    FastAPI backend (runs on the Jetson)
    main.py               routes + websockets
    stats.py              /proc + /sys collector (no jtop needed)
    services.py           systemctl wrapper, "My services" detection
    logs.py               journalctl streaming
    files.py              sandboxed file operations, uploads, zip, tail
    auth.py               Tailscale identity
    audit.py              who-did-what log
    hardware.py           interfaces, USB, serial, cameras, I2C, reachability, event monitor
    preflight.py          checklist engine (checks stored in ~/.local/state/drone-panel/preflight.json)
    ros.py / ros_helper.py  on-demand ROS 2 probe process (exits after 2 min idle)
    terminal.py           PTY sessions that survive disconnects
    camera.py             on-demand MJPEG preview (GStreamer, codec auto-detected via RTSP DESCRIBE)
    flightdata.py         runs/sessions, zip download, delete, notes, cached thumbnails
    gimbal.py             SIYI UDP protocol client; cooperates with siyi-pitch (FOLLOW = siyi-pitch in charge)
    alerts.py             alert rules + history (state in ~/.local/state/drone-panel/alerts.json)
    actions.py            quick actions (state in ~/.local/state/drone-panel/actions.json)
  bin/dpctl               the only privileged helper (see "How access works")
  preflight.default.json  initial checklist for this drone
  actions.default.json    initial quick actions (start/stop pipeline, gimbal flight position)
  web/                    UI (Preact + htm, no build step, all assets vendored)
  config.yaml             default config (installed to /etc/drone-panel/config.yaml)
  install.sh / uninstall.sh
  drone-panel.service     systemd unit with CPU/RAM caps
```

## Deploy

One time, from the laptop: `ssh-copy-id orin@orin`

Then, every time you want to install or update:

```bash
./deploy.sh            # or ./deploy.sh orin@<tailscale-ip>
```

It prints the URL, e.g. `https://orin.<tailnet>.ts.net`.

## How access works

- The agent listens only on `127.0.0.1:8080`. Nothing is exposed on Wi-Fi or Ethernet.
- `tailscale serve` publishes it over HTTPS **inside your tailnet only**, and tells the agent which
  Tailscale user is connecting. Everyone in the tailnet gets full access. To limit it, list emails
  in `auth.allowed_users`.
- The panel runs as `orin`, not root. Its only privileged access is `/opt/drone-panel/bin/dpctl`
  (via `/etc/sudoers.d/drone-panel`), which can only start/stop/restart/enable/disable existing,
  root-owned system units, or open a shell **as orin**. The file manager can only touch what
  `orin` can, and only inside `files.roots`.
- Web-terminal shells run in their own transient systemd units (outside the panel's resource caps).
- **Teammates:** invite them to the tailnet (Tailscale admin → Users), then they use the same HTTPS URL.
  Device sharing to external users did not work for HTTPS (traffic never arrived). If ever needed, set
  `server.tailscale_direct_port: 8080` to add an http listener on the Tailscale IP only; there users are
  identified solely by `tailscale whois` of the connecting address (headers ignored). It is off by default.

## Resource use

The service runs with `CPUQuota=25%` (a quarter of one core, about 4% of the Orin Nano CPU), `MemoryMax=450M` (room for the on-demand ROS 2 probe),
`Nice=10`, and low IO priority, so it cannot starve flight or vision processes. Stats are sampled every 5 s
while nobody is viewing and every 1 s while the dashboard is open.

## Configure

Edit `/etc/drone-panel/config.yaml` on the Jetson (you can do that from the panel's file manager
if you add `/etc/drone-panel` to `files.roots`), then `sudo systemctl restart drone-panel`.

## Develop locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt
cd agent && DRONE_PANEL_AUTH=none ../.venv/bin/uvicorn app.main:app --port 8099 --reload
# open http://127.0.0.1:8099
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "Not authorized" page | Open the `https://….ts.net` URL, not `127.0.0.1`. Tagged devices have no user identity, so use a user-owned device. |
| Start/stop fails | Check that `/etc/sudoers.d/drone-panel` exists (`sudo cat` it), and re-run `install.sh`. |
| ROS 2 page empty | Check `ros.setup` in config.yaml points at your setup.bash files and that your nodes use the default ROS_DOMAIN_ID (or set `ros.domain_id`). |
| Logs page is empty | The `orin` user must be in the `systemd-journal` group. `install.sh` adds it; reboot or restart the service. |
| HTTPS serve failed | Enable **HTTPS Certificates** in the Tailscale admin console (DNS page), then re-run `install.sh`. |
| Panel down | `journalctl -u drone-panel -n 50`, or `sudo systemctl restart drone-panel` over SSH. |

Remove everything: `sudo ~/drone-panel-src/uninstall.sh` (add `--purge` to also delete the config).
