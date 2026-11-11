#!/usr/bin/env bash
# Removes Drone Panel. Your config in /etc/drone-panel is kept unless you pass --purge.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./uninstall.sh"; exit 1; }

PORT=$(grep -E '^\s*port:' /etc/drone-panel/config.yaml 2>/dev/null | head -1 | grep -oE '[0-9]+' || echo 8080)
systemctl disable --now drone-panel.service 2>/dev/null || true
rm -f /etc/systemd/system/drone-panel.service /etc/polkit-1/rules.d/50-drone-panel.rules /etc/sudoers.d/drone-panel
systemctl daemon-reload
if command -v tailscale >/dev/null; then
  tailscale serve --https=443 off 2>/dev/null || tailscale serve --http=80 off 2>/dev/null || true
fi
rm -rf /opt/drone-panel
[[ "${1:-}" == "--purge" ]] && rm -rf /etc/drone-panel
echo "Drone Panel removed."
