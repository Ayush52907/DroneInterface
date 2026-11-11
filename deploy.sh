#!/usr/bin/env bash
# Push the agent from this laptop to the Jetson and (re)install it.
#   ./deploy.sh                # uses orin@orin (Tailscale MagicDNS name)
#   ./deploy.sh orin@<jetson-tailscale-ip>
# Needs SSH key access (one-time: ssh-copy-id orin@orin). Asks for the sudo password on the Jetson.
set -euo pipefail
TARGET="${1:-orin@orin}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "→ Copying agent to $TARGET:~/drone-panel-src"
tar czf - -C "$HERE/agent" --exclude=__pycache__ --exclude='*.pyc' . \
  | ssh "$TARGET" 'rm -rf ~/drone-panel-src && mkdir -p ~/drone-panel-src && tar xzf - -C ~/drone-panel-src'

echo "→ Installing (sudo password on the Jetson may be requested)"
ssh -t "$TARGET" 'cd ~/drone-panel-src && chmod +x install.sh uninstall.sh && sudo ./install.sh'
