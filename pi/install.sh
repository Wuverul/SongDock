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

# Shared screen state for every job. Owned by the login user so manual runs
# and the prepare job can write it; root (sync, boot) can write it anyway.
install -d -o "$LOGIN_USER" -g "$(id -gn "$LOGIN_USER")" -m 755 /var/lib/musicdock

for unit in musicdock-sync@.service musicdock-boot.service; do
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@HOME@|$USER_HOME|g" \
    "$APP_DIR/pi/$unit" > "/etc/systemd/system/$unit"
done
install -m 644 "$APP_DIR/pi/99-musicdock.rules" /etc/udev/rules.d/99-musicdock.rules

systemctl daemon-reload
systemctl enable musicdock-boot.service
udevadm control --reload

echo "Installed. The boot screen appears on the next power-on."
echo "Plug in the player, then watch the log with:"
echo "  journalctl -u 'musicdock-sync@*' -f"
