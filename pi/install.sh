#!/usr/bin/env bash
# Set up (or update) a Music Dock on this Pi. Run from the unpacked folder:
#   sudo ./pi/install.sh           install / update
#   sudo ./pi/install.sh --check   only report what would change
# Safe to run again: every step checks first and only acts if needed.
# (On the developer's Mac, deploy.sh --install runs this over SSH.)
set -euo pipefail

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGIN_USER="${SUDO_USER:-root}"
USER_HOME="$(getent passwd "$LOGIN_USER" | cut -d: -f6)"
USER_GROUP="$(id -gn "$LOGIN_USER")"
CONFIG="$USER_HOME/.config/musicdock/config.toml"
VENV="$USER_HOME/musicdock/venv"
REBOOT_NEEDED=0
TODO=()

say()  { echo "== $*"; }
plan() { echo "   would: $*"; }

# 1. The system ---------------------------------------------------------------
say "Checking the system"
if [ "$LOGIN_USER" = "root" ]; then
  echo "Run this with sudo from your normal user (not as root directly)." >&2
  exit 1
fi
if ! command -v nmcli >/dev/null || ! systemctl is-active --quiet NetworkManager; then
  echo "NetworkManager isn't running. Music Dock needs Raspberry Pi OS Bookworm" >&2
  echo "or newer (which uses NetworkManager for Wi-Fi)." >&2
  exit 1
fi
grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null \
  || echo "   note: this doesn't look like a Raspberry Pi; continuing anyway"

# 2. System packages (only the missing ones) -----------------------------------
PACKAGES="ffmpeg rsync eject python3-pil python3-gpiozero python3-lgpio python3-spidev
          python3-venv fonts-dejavu-core"
MISSING=""
for pkg in $PACKAGES; do
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" \
    || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
  say "Installing system packages:$MISSING"
  if [ $CHECK = 1 ]; then plan "apt-get install$MISSING"; else
    apt-get update -q
    # shellcheck disable=SC2086
    apt-get install -y -q $MISSING
  fi
else
  say "System packages: all present"
fi

# 3. SPI (the display) ----------------------------------------------------------
if command -v raspi-config >/dev/null && [ "$(raspi-config nonint get_spi)" != "0" ]; then
  say "Enabling SPI for the e-ink display (takes effect after a reboot)"
  if [ $CHECK = 1 ]; then plan "raspi-config nonint do_spi 0"; else
    raspi-config nonint do_spi 0
  fi
  REBOOT_NEEDED=1
else
  say "SPI: enabled"
fi

# 3b. Keep the logs across restarts -----------------------------------------------
# Raspberry Pi OS keeps the journal in memory only (Storage=volatile), so a
# freeze or power cut wipes the evidence of what went wrong. Keep it on the SD
# card, capped at 50 MB; journald writes in batches, so wear stays low.
JOURNAL_CONF=/etc/systemd/journald.conf.d/50-musicdock-persistent.conf
JOURNAL_WANT="$(printf '%s\n' "# Installed by Music Dock: keep logs across restarts (capped)." \
  "[Journal]" "Storage=persistent" "SystemMaxUse=50M")"
if [ "$(cat "$JOURNAL_CONF" 2>/dev/null)" != "$JOURNAL_WANT" ]; then
  say "Keeping logs across restarts (up to 50 MB on the SD card)"
  if [ $CHECK = 1 ]; then plan "write $JOURNAL_CONF and restart journald"; else
    install -d -m 755 /etc/systemd/journald.conf.d
    printf '%s\n' "$JOURNAL_WANT" > "$JOURNAL_CONF"
    install -d -m 2755 -g systemd-journal /var/log/journal
    systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
    systemctl restart systemd-journald
    journalctl --flush >/dev/null 2>&1 || true
  fi
else
  say "Logs: kept across restarts"
fi

# 4. Python packages not in apt (pygpod, mutagen) in a venv --------------------
# The venv also sees apt's Pillow/gpiozero (--system-site-packages).
# Raspberry Pi OS blocks system-wide pip installs.
say "Python environment: $VENV"
if [ $CHECK = 1 ]; then
  [ -x "$VENV/bin/python" ] || plan "create the venv"
  plan "pip install -r pi/requirements.txt (pygpod, mutagen)"
else
  [ -x "$VENV/bin/python" ] \
    || sudo -u "$LOGIN_USER" python3 -m venv --system-site-packages "$VENV"
  sudo -u "$LOGIN_USER" "$VENV/bin/pip" install -q --disable-pip-version-check \
    -r "$APP_DIR/pi/requirements.txt"
fi

# 5. Config (created from the template only if there isn't one) ----------------
if [ -f "$CONFIG" ]; then
  say "Config: $CONFIG (kept as is)"
else
  say "Creating your config: $CONFIG"
  if [ $CHECK = 1 ]; then plan "copy config.example.toml there (mode 600)"; else
    install -d -o "$LOGIN_USER" -g "$USER_GROUP" -m 700 "$(dirname "$CONFIG")"
    install -o "$LOGIN_USER" -g "$USER_GROUP" -m 600 "$APP_DIR/config.example.toml" "$CONFIG"
  fi
  TODO+=("Edit $CONFIG: your Navidrome url, username and password (nano $CONFIG)")
fi

# 6. Services, udev rule, Wi-Fi hook -------------------------------------------
say "Installing services"
if [ $CHECK = 1 ]; then
  plan "install systemd units, the udev rule, the NetworkManager hook, musicdock-status and /run/musicdock"
else
  # Shared screen state for every job. Owned by the login user so manual runs
  # and the prepare job can write it; root (sync, boot) can write it anyway.
  install -d -o "$LOGIN_USER" -g "$USER_GROUP" -m 755 /var/lib/musicdock
  # Scratch files that change often (the last screen image) live in RAM, not
  # on the SD card. /run is emptied at boot, so systemd recreates the folder.
  printf 'd /run/musicdock 0755 %s %s -\n' "$LOGIN_USER" "$USER_GROUP" \
    > /etc/tmpfiles.d/musicdock.conf
  systemd-tmpfiles --create /etc/tmpfiles.d/musicdock.conf
  rm -f /var/lib/musicdock/dock_status.frame.png       # older versions kept it here
  for unit in musicdock-sync@.service musicdock-boot.service \
              musicdock-prepare.service musicdock-prepare.timer \
              musicdock-wifi-refresh.service musicdock-rest@.service \
              musicdock-resync.service musicdock-resync.path; do
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@HOME@|$USER_HOME|g" \
        -e "s|@USER@|$LOGIN_USER|g" \
      "$APP_DIR/pi/$unit" > "/etc/systemd/system/$unit"
  done
  install -m 644 "$APP_DIR/pi/99-musicdock.rules" /etc/udev/rules.d/99-musicdock.rules
  # NetworkManager only runs dispatcher hooks owned by root and not writable by others.
  install -o root -g root -m 755 "$APP_DIR/pi/90-musicdock-wifi" \
    /etc/NetworkManager/dispatcher.d/90-musicdock-wifi
  # Status report command, also usable over SSH: ssh user@dock musicdock-status
  cat > /usr/local/bin/musicdock-status <<EOF
#!/bin/sh
# Music Dock status report (read-only). Installed by $APP_DIR/pi/install.sh.
MUSICDOCK_CONFIG="\${MUSICDOCK_CONFIG:-$USER_HOME/.config/musicdock/config.toml}" \\
  exec "$VENV/bin/python" "$APP_DIR/dock_report.py" "\$@"
EOF
  chmod 755 /usr/local/bin/musicdock-status
  systemctl daemon-reload
  systemctl enable musicdock-boot.service
  systemctl enable --now musicdock-prepare.timer
  systemctl enable --now musicdock-resync.path
  udevadm control --reload
fi

# 7. What's next ----------------------------------------------------------------
echo
if [ $CHECK = 1 ]; then
  echo "Check only: nothing was changed."
else
  echo "Music Dock is installed in $APP_DIR."
fi
if [ ${#TODO[@]} -gt 0 ]; then
  echo "Next:"
  for t in "${TODO[@]}"; do echo "  - $t"; done
fi
if [ $REBOOT_NEEDED = 1 ]; then
  echo "  - Reboot so SPI (the display) turns on:  sudo reboot"
fi
echo "Watch the dock's log with:"
echo "  journalctl -u 'musicdock-*' -f"
