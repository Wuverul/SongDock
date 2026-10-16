#!/usr/bin/env bash
# Install the udev rule and systemd service on the Pi.
# Run on the Pi from the deployed folder:  sudo ./pi/install.sh
# (deploy.sh --install does this for you.)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGIN_USER="${SUDO_USER:-root}"
USER_HOME="$(getent passwd "$LOGIN_USER" | cut -d: -f6)"

for cmd in python3 lsblk mount umount; do
  command -v "$cmd" >/dev/null || { echo "Missing $cmd" >&2; exit 1; }
done

sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@HOME@|$USER_HOME|g" \
  "$APP_DIR/pi/musicdock-sync@.service" > /etc/systemd/system/musicdock-sync@.service
install -m 644 "$APP_DIR/pi/99-musicdock.rules" /etc/udev/rules.d/99-musicdock.rules

systemctl daemon-reload
udevadm control --reload

echo "Installed. Plug in the player, then watch the log with:"
echo "  journalctl -u 'musicdock-sync@*' -f"
