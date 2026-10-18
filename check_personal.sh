#!/usr/bin/env bash
# Look for personal details (hosts, addresses, device IDs, names) before
# publishing. The patterns live in .personal-patterns (one extended regex per
# line, any case; git-ignored, so the list itself stays private).
#
#   ./check_personal.sh                 tracked files as they are now
#   ./check_personal.sh --history       every version of every file, commit
#                                       messages and authors (before a push)
#   ./check_personal.sh --install-hook  run --history before every git push
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--install-hook" ]; then
  hook="$(git rev-parse --git-path hooks)/pre-push"
  printf '#!/bin/sh\n# Installed by check_personal.sh: never push personal details.\nexec "%s" --history\n' \
    "$PWD/check_personal.sh" > "$hook"
  chmod +x "$hook"
  echo "Installed $hook"
  exit 0
fi

if [ ! -f .personal-patterns ]; then
  echo "No .personal-patterns file: nothing to check against." >&2
  exit 1
fi
patterns="$(grep -vE '^[[:space:]]*(#|$)' .personal-patterns)"

if [ "${1:-}" = "--history" ]; then
  found="$(git log --all -p --format='commit %h%nAuthor: %an <%ae>%nCommitter: %cn <%ce>%n%n%B' \
           | grep -niE -f <(printf '%s\n' "$patterns") || true)"
  where="the history"
else
  found="$(git grep -nIiE -f <(printf '%s\n' "$patterns") || true)"
  where="tracked files"
fi

if [ -n "$found" ]; then
  echo "Personal details found in $where:" >&2
  printf '%s\n' "$found" | cut -c1-160 | head -40 >&2
  exit 1
fi
echo "No personal details in $where."
