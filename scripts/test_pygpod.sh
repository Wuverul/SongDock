#!/usr/bin/env bash
# Run the test suite against a pygpod version, in a throwaway virtual environment:
#   scripts/test_pygpod.sh            the version the docks install (pi/requirements.txt)
#   scripts/test_pygpod.sh latest     the newest release on PyPI
#   scripts/test_pygpod.sh 0.2.3      that version
# The iPod tests need pygpod, mutagen and Pillow; all three are installed here.
# How to move the docks to a new pygpod: see "Updating pygpod" in CLAUDE.md.
set -euo pipefail
cd "$(dirname "$0")/.."
pinned="$(sed -n 's/^pygpod==//p' pi/requirements.txt)"
want="${1:-$pinned}"
venv="$(mktemp -d)/venv"
trap 'rm -rf "$(dirname "$venv")"' EXIT
python3 -m venv "$venv"
if [ "$want" = latest ]; then spec=pygpod; else spec="pygpod==$want"; fi
"$venv/bin/pip" install -q "$spec" "$(grep '^mutagen==' pi/requirements.txt)" pillow
got="$("$venv/bin/python" -c 'import importlib.metadata as m; print(m.version("pygpod"))')"
echo "== pygpod $got (the docks install $pinned)"
"$venv/bin/python" -W ignore -m unittest discover -s tests
