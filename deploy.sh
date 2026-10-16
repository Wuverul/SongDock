#!/usr/bin/env bash
# Copy the project from the Mac to the Pi over SSH.
#
# Usage:
#   ./deploy.sh user@pi-address             # copy files only
#   ./deploy.sh --install user@pi-address   # copy, then install udev rule + service
#
# Set MUSICDOCK_PI=user@pi-address (e.g. in ~/.zshrc) to skip typing it.
#
# Code goes to ~/musicdock/app on the Pi. --delete removes files there that no
# longer exist on the Mac, so it is kept separate from ~/musicdock/cache.
set -euo pipefail

INSTALL=0
if [ "${1:-}" = "--install" ]; then
  INSTALL=1
  shift
fi

PI="${1:-${MUSICDOCK_PI:-}}"
if [ -z "$PI" ]; then
  echo "Usage: $0 [--install] user@pi-address   (or set MUSICDOCK_PI)" >&2
  exit 1
fi

REMOTE_DIR="musicdock/app"   # relative to the Pi user's home
cd "$(dirname "$0")"

echo "== Checking for private keys"
if keys=$(grep -rlI --exclude-dir=.git -e "-----BEGIN [A-Z ]*PRIVATE KEY-----" . ); then
  echo "Refusing to deploy: these files look like private keys:" >&2
  echo "$keys" >&2
  echo "Move them out of the project folder (SSH keys belong in ~/.ssh)." >&2
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
  --exclude cache --exclude tmp \
  ./ "$PI:$REMOTE_DIR/"

if [ "$INSTALL" -eq 1 ]; then
  echo "== Installing udev rule and service (sudo may ask for the Pi password)"
  ssh -t "$PI" "sudo ~/$REMOTE_DIR/pi/install.sh"
fi

echo "== Done"
