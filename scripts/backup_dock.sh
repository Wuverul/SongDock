#!/usr/bin/env bash
# Back up the dock's settings, its record of the players and its plan to this
# computer, over SSH. Small (a few MB): no music. With these, a dead SD card
# means a fresh install plus copying them back, not starting over (see INSTALL.md,
# "Backups").
#
# Usage:
#   scripts/backup_dock.sh [user@pi-address]             one backup now
#   scripts/backup_dock.sh --install [user@pi-address]   every day at noon (macOS:
#                                                        launchd; a Mac asleep then
#                                                        catches up when it wakes)
#   scripts/backup_dock.sh --uninstall                   stop the daily backups
#
# Set MUSICDOCK_PI=user@pi-address (e.g. in ~/.zshrc) to skip typing it.
# Backups go to ~/SongDock-backups (SONGDOCK_BACKUPS to change); the newest 30 are
# kept (SONGDOCK_BACKUP_KEEP). They hold the server password (config.toml): the
# folder is readable by you only.
set -euo pipefail

DEST="${SONGDOCK_BACKUPS:-$HOME/SongDock-backups}"
KEEP="${SONGDOCK_BACKUP_KEEP:-30}"
LABEL="com.songdock.backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mode=backup
case "${1:-}" in
  --install) mode=install; shift ;;
  --uninstall) mode=uninstall; shift ;;
esac
PI="${1:-${MUSICDOCK_PI:-}}"

if [ "$mode" = uninstall ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Daily backups stopped (backups already made stay in $DEST)"
  exit 0
fi
if [ -z "$PI" ]; then
  echo "Usage: $0 [--install] [user@pi-address]   (or set MUSICDOCK_PI)" >&2
  exit 1
fi

if [ "$mode" = install ]; then
  # macOS doesn't let background jobs read Documents, Desktop or Downloads: the job
  # runs a copy kept in Application Support (--install again after changing it).
  script="$HOME/Library/Application Support/SongDock/backup_dock.sh"
  mkdir -p "$DEST" "$(dirname "$PLIST")" "$(dirname "$script")"
  if [ "$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" != "$script" ]; then
    cp "$0" "$script"
  fi
  chmod 700 "$script"
  chmod 700 "$DEST"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$script</string><string>$PI</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SONGDOCK_BACKUPS</key><string>$DEST</string>
    <key>SONGDOCK_BACKUP_KEEP</key><string>$KEEP</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DEST/backup.log</string>
  <key>StandardErrorPath</key><string>$DEST/backup.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Daily backups at noon to $DEST (log: $DEST/backup.log). Making one now:"
  exec /bin/bash "$script" "$PI"
fi

# What the Pi sends: a .tar.gz on standard output. The record is copied with
# SQLite's own backup (consistent even while a sync writes to it).
REMOTE=$(cat <<'PY'
import glob, io, json, os, sqlite3, sys, tarfile, tempfile, time, tomllib

config = os.environ.get("MUSICDOCK_CONFIG") or os.path.expanduser("~/.config/musicdock/config.toml")
try:
    with open(config, "rb") as f:
        cache = tomllib.load(f).get("sync", {}).get("cache_dir", "~/musicdock/cache")
except (OSError, tomllib.TOMLDecodeError):
    cache = "~/musicdock/cache"
cache = os.path.expanduser(cache)
files = []                                  # (path on the Pi, name in the backup)
for path in glob.glob(os.path.join(os.path.dirname(config), "*.toml")):
    files.append((path, "config/" + os.path.basename(path)))
for path in glob.glob("/var/lib/musicdock/*.json"):
    files.append((path, "var-lib-musicdock/" + os.path.basename(path)))
for path in glob.glob(os.path.join(cache, "*.json")):
    files.append((path, "cache/" + os.path.basename(path)))
with tempfile.TemporaryDirectory() as tmp:
    record = os.path.join(tmp, "dock.db")
    src = sqlite3.connect("file:/var/lib/musicdock/dock.db?mode=ro", uri=True)
    dst = sqlite3.connect(record)
    src.backup(dst)
    dst.close()
    src.close()
    files.append((record, "var-lib-musicdock/dock.db"))
    out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz")
    for path, name in files:
        try:
            out.add(path, arcname=name)
        except OSError as e:
            print(f"skipped {path}: {e}", file=sys.stderr)
    info = json.dumps({"made": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "files": [n for _, n in files]}, indent=1).encode()
    entry = tarfile.TarInfo("backup.json")
    entry.size, entry.mtime = len(info), time.time()
    out.addfile(entry, io.BytesIO(info))
    out.close()
PY
)

mkdir -p "$DEST"
chmod 700 "$DEST"
name="songdock-$(date +%Y%m%d-%H%M%S).tar.gz"
tmp="$DEST/.$name.partial"
trap 'rm -f "$tmp"' EXIT
echo "$(date '+%Y-%m-%d %H:%M:%S') backing up $PI"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$PI" python3 - <<< "$REMOTE" > "$tmp"
tar -tzf "$tmp" | grep -qx "var-lib-musicdock/dock.db" || {
  echo "Backup incomplete (no record in it): not kept" >&2
  exit 1
}
chmod 600 "$tmp"
mv "$tmp" "$DEST/$name"
echo "Saved $DEST/$name ($(du -h "$DEST/$name" | cut -f1))"

# Keep the newest $KEEP.
ls -1t "$DEST"/songdock-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done
