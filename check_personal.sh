#!/usr/bin/env bash
# Look for personal details (hosts, addresses, device IDs, names) before
# publishing. The patterns live in .personal-patterns (one extended regex per
# line, any case; git-ignored, so the list itself stays private).
#
#   ./check_personal.sh                 tracked files as they are now
#   ./check_personal.sh --history       every version of every file, commit
#                                       messages and authors (before a push)
#   ./check_personal.sh --install-hook  run --history before every git push
#
# Each file is checked with its line breaks and indentation collapsed to
# single spaces, so a detail wrapped across two lines ("Home" / "WiFi")
# is still found - a line-by-line search missed exactly that once.
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
patterns="$(mktemp)"
trap 'rm -f "$patterns"' EXIT
# A space in a pattern matches any run of whitespace (text is collapsed anyway).
grep -vE '^[[:space:]]*(#|$)' .personal-patterns | sed 's/ /[[:space:]]+/g' > "$patterns"

scan() {        # stdin: text; $1: label for the report
  tr -s '[:space:]' ' ' | grep -oiE ".{0,40}($(paste -sd'|' "$patterns")).{0,40}" \
    | sed "s|^|$1: |" || true
}

found=""
if [ "${1:-}" = "--history" ]; then
  where="the history"
  # Every blob that was ever committed, with a path it had.
  while read -r blob path; do
    found+="$(git cat-file blob "$blob" | scan "$path ($blob)")"$'\n'
  done < <(git rev-list --all --objects \
           | git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' \
           | awk '$1=="blob"{print $2, $3}' | sort -u -k1,1)
  found+="$(git log --all --format='commit %h%nAuthor: %an <%ae>%nCommitter: %cn <%ce>%n%n%B%n' \
            | scan "commit messages/authors")"
else
  where="tracked files"
  while read -r f; do
    [ -f "$f" ] && found+="$(scan "$f" < "$f")"$'\n'
  done < <(git ls-files)
fi

found="$(printf '%s\n' "$found" | grep -v '^$' || true)"
if [ -n "$found" ]; then
  echo "Personal details found in $where:" >&2
  printf '%s\n' "$found" | cut -c1-200 | head -40 >&2
  exit 1
fi
echo "No personal details in $where."
