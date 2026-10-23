#!/usr/bin/env bash
# Build the package to give to someone setting up their own dock:
#   ./make_release.sh   ->  dist/songdock/  and  dist/songdock-<version>.tar.gz
# The version comes from the VERSION file (e.g. 0.5.0); tag it as v<version>.
# Contains only what runs on the Pi plus the docs; refuses to build if the
# tests fail or anything personal (servers, hosts, IDs, emails) slipped in.
set -euo pipefail
cd "$(dirname "$0")"

NAME="songdock"
VERSION="$(tr -d '[:space:]' < VERSION)"
OUT="dist/$NAME"
TARBALL="dist/$NAME-$VERSION.tar.gz"

echo "== Running tests"
python3 -m unittest discover -s tests -q

echo "== Assembling $OUT"
rm -rf "$OUT" "$TARBALL"
mkdir -p "$OUT"
cp README.md INSTALL.md LICENSE CHANGELOG.md config.example.toml status.sh "$OUT/"
cp dock_*.py ipod_handler.py rockbox_handler.py subsonic.py "$OUT/"
mkdir -p "$OUT/pi"
cp pi/install.sh pi/requirements.txt pi/99-musicdock.rules pi/90-musicdock-wifi \
   pi/*.service pi/*.timer pi/*.path "$OUT/pi/"
cp -R vendor "$OUT/vendor"
find "$OUT" -name '__pycache__' -prune -exec rm -rf {} + -o -name '.DS_Store' -delete
chmod +x "$OUT/pi/install.sh" "$OUT/pi/90-musicdock-wifi"
printf 'SongDock %s (%s, %s)\n' "$VERSION" "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  "$(date +%Y-%m-%d)" \
  > "$OUT/VERSION"

echo "== Checking for personal data"
# The developer's own hosts, IDs and names live in .personal-patterns (one
# extended regex per line; git-ignored, so the list itself stays private).
if [ -f .personal-patterns ]; then
  if grep -rnIiE -f <(grep -vE '^\s*(#|$)' .personal-patterns) "$OUT"; then
    echo "Refusing to build: personal data found above." >&2
    exit 1
  fi
else
  echo "   note: no .personal-patterns file; skipping the personal-data check"
fi
if grep -rlI -e "-----BEGIN [A-Z ]*PRIVATE KEY-----" "$OUT"; then
  echo "Refusing to build: private key found." >&2
  exit 1
fi
grep -q 'password = "your-navidrome-password"' "$OUT/config.example.toml" \
  || { echo "Refusing to build: config.example.toml has a real password" >&2; exit 1; }

echo "== Packing $TARBALL"
# No macOS metadata (it makes Linux tar print confusing warnings).
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf "$TARBALL" -C dist "$NAME"
echo
echo "Done: $TARBALL ($(du -h "$TARBALL" | cut -f1)), $(find "$OUT" -type f | wc -l | tr -d ' ') files"
echo "Give it to them with INSTALL.md (it's also inside the package)."
