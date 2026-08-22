#!/usr/bin/env bash
#
# Runs doctor.py in whatever terminal the desktop opened, then waits.
#
# Without the wait the terminal closes the instant the checks finish, which
# is exactly when there is something to read. This exists as its own script
# because a .desktop Exec line cannot hold a shell pipeline.

set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$HERE" || exit 1

PY="$HERE/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 2>/dev/null || true)"

if [ -z "$PY" ]; then
    printf 'No Python interpreter found in %s/venv or on PATH.\n' "$HERE"
else
    "$PY" "$HERE/doctor.py"
fi

printf '\n----\nPress Enter to close this window.\n'
read -r _
