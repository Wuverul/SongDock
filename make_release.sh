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
cp ./*.py "$OUT/"                          # every module in the top folder
mkdir -p "$OUT/pi"
[ -f pi/release_signers ] && cp pi/release_signers "$OUT/pi/"     # for dock_update.py
cp pi/install.sh pi/requirements.txt pi/99-musicdock.rules pi/90-musicdock-wifi \
   pi/*.service pi/*.timer pi/*.path "$OUT/pi/"
cp -R vendor "$OUT/vendor"
mkdir -p "$OUT/docs" && cp docs/*.png "$OUT/docs/"       # the README's screen images
find "$OUT" -name '__pycache__' -prune -exec rm -rf {} + -o -name '.DS_Store' -delete
chmod +x "$OUT/pi/install.sh" "$OUT/pi/90-musicdock-wifi"
printf 'SongDock %s (%s, %s)\n' "$VERSION" "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  "$(date +%Y-%m-%d)" \
  > "$OUT/VERSION"

echo "== Checking every module is in the package"
# A hand-kept list once left out ipod_db.py (caught before the 0.7.0 build
# shipped): every Python file git tracks in the top folder must be here.
if git rev-parse --git-dir >/dev/null 2>&1; then
  missing="$(git ls-files '*.py' | grep -v / | while read -r f; do
               [ -f "$OUT/$f" ] || echo "$f"; done)"
  if [ -n "$missing" ]; then
    echo "Refusing to build: missing from the package: $missing" >&2
    exit 1
  fi
fi

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

# Docks install a release only with a signature from this key (dock_update.py).
KEY="${SONGDOCK_RELEASE_KEY:-$HOME/.ssh/songdock-release}"
if [ -f "$KEY" ]; then
  echo "== Signing $TARBALL"
  rm -f "$TARBALL.sig"
  ssh-keygen -q -Y sign -f "$KEY" -n songdock-release "$TARBALL"
  ssh-keygen -q -Y verify -f pi/release_signers -I songdock-release -n songdock-release \
    -s "$TARBALL.sig" < "$TARBALL" >/dev/null
  echo "Upload both to the release: $TARBALL $TARBALL.sig"
else
  echo "Not signed (no $KEY): docks won't install this one by themselves." >&2
fi
