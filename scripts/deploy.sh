#!/usr/bin/env bash
# Copy the project from the Mac to the Pi over SSH.
#
# Usage:
#   scripts/deploy.sh user@pi-address
#
# Set MUSICDOCK_PI=user@pi-address (e.g. in ~/.zshrc) to skip typing it.
#
# Copies to ~/musicdock/app on the Pi, then runs its installer with sudo (asks for
# the Pi password), which installs the code in /opt/songdock, owned by root. The
# copy in your home is only the source: nothing runs from it. --delete removes
# files there that no longer exist on the Mac. --install is accepted, as before.
set -euo pipefail

[ "${1:-}" = "--install" ] && shift         # every deploy installs now

PI="${1:-${MUSICDOCK_PI:-}}"
if [ -z "$PI" ]; then
  echo "Usage: $0 user@pi-address   (or set MUSICDOCK_PI)" >&2
  exit 1
fi

REMOTE_DIR="musicdock/app"   # relative to the Pi user's home
cd "$(dirname "$0")/.."

echo "== Checking for private keys"
if keys=$(grep -rlI --exclude-dir=.git -e "-----BEGIN [A-Z ]*PRIVATE KEY-----" . ); then
  echo "Refusing to deploy: these files look like private keys:" >&2
  echo "$keys" >&2
  echo "Move them out of the project folder (SSH keys belong in ~/.ssh)." >&2
  exit 1
fi

if ! grep -q 'password = "your-navidrome-password"' config.example.toml; then
  echo "Refusing to deploy: config.example.toml no longer has the placeholder" >&2
  echo "password. Real credentials go on the Pi in ~/.config/musicdock/config.toml." >&2
  exit 1
fi

echo "== Running tests"
python3 -m unittest discover -s tests -q

echo "== Checking the Pi"
ssh "$PI" "mkdir -p ~/$REMOTE_DIR"
if ! ssh "$PI" "command -v rsync" >/dev/null; then
  echo "rsync isn't installed on the Pi. On the Pi run: sudo apt install rsync" >&2
  exit 1
fi

echo "== Copying to $PI:~/$REMOTE_DIR"
rsync -av --delete \
  --exclude .git --exclude .DS_Store \
  --exclude __pycache__ --exclude '*.pyc' \
  --exclude previews --exclude '*.png' \
  --exclude config.toml --exclude '*.local.toml' --exclude .env \
  --exclude cache --exclude tmp --exclude TASKS.md \
  ./ "$PI:$REMOTE_DIR/"

echo "== Installing in /opt/songdock (sudo asks for the Pi password)"
ssh -t "$PI" "sudo ~/$REMOTE_DIR/pi/install.sh"

echo "== Done"
