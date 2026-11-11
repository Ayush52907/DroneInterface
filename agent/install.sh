#!/usr/bin/env bash
# Drone Panel installer. Run on the Jetson from the folder containing this file:
#   sudo ./install.sh            # install or update
# Re-running is safe: code is updated, your /etc/drone-panel/config.yaml is kept.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/drone-panel
CONF_DIR=/etc/drone-panel
PORT_DEFAULT=8080
RUN_USER="${PANEL_USER:-${SUDO_USER:-}}"

c_ok() { printf '\033[32m✔\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
c_err() { printf '\033[31m✘\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { c_err "Run with sudo: sudo ./install.sh"; exit 1; }
[[ -n "$RUN_USER" && "$RUN_USER" != root ]] || { c_err "Run via sudo from your normal user (or set PANEL_USER=orin)"; exit 1; }
id "$RUN_USER" >/dev/null || { c_err "User $RUN_USER not found"; exit 1; }
echo "Installing Drone Panel to run as user: $RUN_USER"

# ---------------------------------------------------------------- packages
PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
TMPV=$(mktemp -d)
if ! python3 -m venv "$TMPV/v" >/dev/null 2>&1; then
  echo "Installing python3-venv…"
  apt-get update -qq && apt-get install -y -qq "python${PYV}-venv" || apt-get install -y -qq python3-venv
fi
rm -rf "$TMPV"
c_ok "Python $PYV with venv"

# ---------------------------------------------------------------- code + venv
mkdir -p "$DEST"
rm -rf "$DEST/app" "$DEST/web"
rm -rf "$DEST/bin"
cp -r "$SRC/app" "$SRC/web" "$SRC/bin" "$SRC/requirements.txt" "$SRC/preflight.default.json" "$SRC/actions.default.json" "$DEST/"
sed -i "s/__USER__/$RUN_USER/g" "$DEST/bin/dpctl"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
if [[ ! -x "$DEST/.venv/bin/python" ]]; then
  python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
chown -R root:root "$DEST"
chmod -R go-w "$DEST"
chmod 755 "$DEST/bin/dpctl"
c_ok "Code and Python packages in $DEST"

# ---------------------------------------------------------------- config
mkdir -p "$CONF_DIR"
if [[ ! -f "$CONF_DIR/config.yaml" ]]; then
  cp "$SRC/config.yaml" "$CONF_DIR/config.yaml"
  c_ok "Created $CONF_DIR/config.yaml"
else
  # add any new top-level sections from the shipped config, keep everything you changed
  "$DEST/.venv/bin/python" - "$SRC/config.yaml" "$CONF_DIR/config.yaml" <<'PY'
import re, sys, yaml
new_txt = open(sys.argv[1]).read(); cur_path = sys.argv[2]
cur = yaml.safe_load(open(cur_path)) or {}
blocks = re.split(r"(?m)^(?=[A-Za-z_][\w-]*:)", new_txt)
added = []
for b in blocks:
    m = re.match(r"([A-Za-z_][\w-]*):", b)
    if m and m.group(1) not in cur:
        open(cur_path, "a").write("\n" + b.rstrip() + "\n"); added.append(m.group(1))
print("added config sections: " + ", ".join(added) if added else "config up to date")
PY
  c_ok "Kept existing $CONF_DIR/config.yaml"
fi
chown "$RUN_USER":"$RUN_USER" "$CONF_DIR/config.yaml"
PORT=$(grep -E '^\s*port:' "$CONF_DIR/config.yaml" | head -1 | grep -oE '[0-9]+' || echo $PORT_DEFAULT)

# ---------------------------------------------------------------- permissions
for g in systemd-journal adm; do getent group $g >/dev/null && usermod -aG $g "$RUN_USER"; done
rm -f /etc/polkit-1/rules.d/50-drone-panel.rules   # replaced by dpctl (older versions)
SUDOERS=/etc/sudoers.d/drone-panel
cat > "$SUDOERS.tmp" <<EOF
# Drone Panel: the panel user may run only dpctl as root (see $DEST/bin/dpctl)
Cmnd_Alias DRONE_PANEL_OWNERS = $DEST/bin/dpctl owners
# the read-only "owners" scan is polled; keep it out of the auth log
Defaults!DRONE_PANEL_OWNERS !log_allowed, !pam_session
$RUN_USER ALL=(root) NOPASSWD: $DEST/bin/dpctl
EOF
chmod 440 "$SUDOERS.tmp"
if visudo -cf "$SUDOERS.tmp" >/dev/null; then
  mv "$SUDOERS.tmp" "$SUDOERS"
  c_ok "sudoers: $RUN_USER may run only $DEST/bin/dpctl (service control + terminal as $RUN_USER)"
else
  rm -f "$SUDOERS.tmp"; c_err "sudoers validation failed"; exit 1
fi

# ---------------------------------------------------------------- systemd
sed "s/__USER__/$RUN_USER/g" "$SRC/drone-panel.service" > /etc/systemd/system/drone-panel.service
systemctl daemon-reload
systemctl enable drone-panel.service >/dev/null 2>&1
systemctl restart drone-panel.service
for _ in $(seq 1 20); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
  systemctl is-failed --quiet drone-panel.service && break
  sleep 1
done
if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" | grep -q 200; then
  c_ok "drone-panel.service running, answering on 127.0.0.1:$PORT"
else
  c_err "drone-panel.service is not answering on 127.0.0.1:$PORT:"
  journalctl -u drone-panel.service -n 30 --no-pager
  exit 1
fi

# ---------------------------------------------------------------- tailscale serve
URL=""
if command -v tailscale >/dev/null; then
  DNS=$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
  echo "Setting up Tailscale Serve. If Serve/HTTPS is not enabled on your tailnet yet,"
  echo "Tailscale prints a link below: open it, approve, and this script continues."
  if timeout 600 tailscale serve --bg --https=443 "http://127.0.0.1:$PORT" 2>&1 | tee /tmp/dp-serve.log; [[ ${PIPESTATUS[0]} -eq 0 ]]; then
    URL="https://$DNS"
    c_ok "Tailscale Serve (HTTPS) → 127.0.0.1:$PORT"
  else
    c_warn "Tailscale Serve was not set up (not approved within 10 min, or HTTPS unavailable)."
    c_warn "The panel is running. Approve Serve in the Tailscale admin console, then re-run ./deploy.sh"
  fi
else
  c_warn "tailscale not installed: panel only reachable on 127.0.0.1"
fi

echo
echo "────────────────────────────────────────────────────────────"
[[ -n "$URL" ]] && echo "  Open:   $URL" || true
DPORT=$(grep -E '^\s*tailscale_direct_port:' "$CONF_DIR/config.yaml" | head -1 | grep -oE '[0-9]+' || true)
TSIP=$(tailscale ip -4 2>/dev/null | head -1)
[[ -n "$DPORT" && -n "$TSIP" ]] && echo "  Also (shared users): http://$TSIP:$DPORT" || true
echo "  Config: $CONF_DIR/config.yaml   (then: sudo systemctl restart drone-panel)"
echo "  Logs:   journalctl -u drone-panel -f"
echo "────────────────────────────────────────────────────────────"
