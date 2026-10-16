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

# Python packages not in apt (pygpod, mutagen) live in a venv owned by the
# login user; it also sees apt's Pillow/gpiozero. Raspberry Pi OS blocks
# system-wide pip installs.
VENV="$USER_HOME/musicdock/venv"
if [ ! -x "$VENV/bin/python" ]; then
  sudo -u "$LOGIN_USER" python3 -m venv --system-site-packages "$VENV"
fi
sudo -u "$LOGIN_USER" "$VENV/bin/pip" install -q --disable-pip-version-check \
  -r "$APP_DIR/pi/requirements.txt"

for unit in musicdock-sync@.service musicdock-boot.service \
            musicdock-prepare.service musicdock-prepare.timer \
            musicdock-wifi-refresh.service musicdock-rest@.service; do
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@HOME@|$USER_HOME|g" \
      -e "s|@USER@|$LOGIN_USER|g" \
    "$APP_DIR/pi/$unit" > "/etc/systemd/system/$unit"
done
install -m 644 "$APP_DIR/pi/99-musicdock.rules" /etc/udev/rules.d/99-musicdock.rules
# NetworkManager only runs dispatcher hooks owned by root and not writable by others.
install -o root -g root -m 755 "$APP_DIR/pi/90-musicdock-wifi" \
  /etc/NetworkManager/dispatcher.d/90-musicdock-wifi

command -v ffmpeg >/dev/null || echo "Note: ffmpeg is missing; run: sudo apt install ffmpeg"

systemctl daemon-reload
systemctl enable musicdock-boot.service
systemctl enable --now musicdock-prepare.timer
udevadm control --reload

echo "Installed. The boot screen appears on the next power-on."
echo "Plug in the player, then watch the log with:"
echo "  journalctl -u 'musicdock-sync@*' -f"
