#!/usr/bin/env bash
# Show the dock's status from the Mac (runs musicdock-status on the Pi over SSH).
#
# Usage:
#   scripts/status.sh [user@pi-address] [--since "3 days ago"]
#
# Set MUSICDOCK_PI=user@pi-address (e.g. in ~/.zshrc) to skip typing it.
set -euo pipefail

PI="${MUSICDOCK_PI:-}"
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then
  PI="$1"
  shift
fi
if [ -z "$PI" ]; then
  echo "Usage: $0 [user@pi-address] [--since \"3 days ago\"]   (or set MUSICDOCK_PI)" >&2
  exit 1
fi

# Quote each argument for the remote shell ("3 days ago" stays one argument).
exec ssh -o ConnectTimeout=10 "$PI" musicdock-status ${*:+$(printf '%q ' "$@")}
