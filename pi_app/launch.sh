#!/usr/bin/env bash
#
# SafetyFirst checkpoint — what the desktop icon runs.
#
# A gate is an appliance, so this behaves like one: tap the icon and the
# display comes up fullscreen with no terminal behind it. That convenience
# is also the hazard — when something goes wrong there is no console to
# print to, so a failure would simply be an icon that does nothing. Every
# exit path here therefore ends in either a running gate or a message on
# screen saying why not.
#
#   ./launch.sh              run once (what the icon does)
#   ./launch.sh --supervise  restart on crash (what autostart does)
#
# Config is NOT sourced here: checkpoint.py loads .env itself with
# python-dotenv, which parses it properly. Sourcing it as shell would run
# the file, and a password containing a backtick or $( would do something
# other than be a password.

set -u

# readlink -f so the launcher still resolves when the .desktop entry points
# at a symlink in ~/Desktop rather than at the file in the repo.
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$HERE" || exit 1

LOG="${SAFETYFIRST_LOG:-$HERE/checkpoint.log}"
LOCK="$HERE/.checkpoint.lock"
SUPERVISE=0
[ "${1:-}" = "--supervise" ] && SUPERVISE=1

# -- telling a person something, with no terminal to print to --------------
notify() {
    title="$1"
    # %b so the \n in the messages below become real line breaks. zenity
    # and xmessage both render the escape literally otherwise, which turns
    # a set of install steps into one unreadable line.
    body="$(printf '%b' "$2")"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --width=460 --title="$title" --text="$body" 2>/dev/null
    elif command -v xmessage >/dev/null 2>&1; then
        printf '%s\n\n%s\n' "$title" "$body" | xmessage -center -file - 2>/dev/null
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "$title" "$body"
    fi
    printf '%s: %s\n' "$title" "$body" >&2
}

# -- one gate at a time ----------------------------------------------------
# Two copies would fight over the camera and over the master's serial port,
# and the loser fails in a way that reads as broken hardware. The lock is
# held on a file descriptor, so it is released by the kernel however this
# script ends - including being killed.
exec 9>"$LOCK" || exit 1
if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        notify "SafetyFirst is already running" \
               "The checkpoint is open on this screen already. Close that window before starting it again."
        exit 0
    fi
fi

# -- interpreter -----------------------------------------------------------
PY="$HERE/venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 2>/dev/null || true)"
fi
if [ -z "$PY" ]; then
    notify "SafetyFirst cannot start" \
           "No Python interpreter was found.\n\nInstall it, then create the app environment:\n  cd $HERE\n  python3 -m venv venv\n  venv/bin/pip install -r requirements.txt"
    exit 1
fi

# tkinter is a separate apt package on Raspberry Pi OS and its absence is
# the single most common reason this app will not start. Say so by name
# rather than letting a traceback scroll past in a log nobody opens.
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    notify "SafetyFirst cannot start" \
           "Python is missing tkinter, which draws the gate display.\n\nInstall it with:\n  sudo apt install -y python3-tk"
    exit 1
fi

# -- run -------------------------------------------------------------------
{
    printf '\n===== %s : starting (%s) =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$PY"
} >>"$LOG" 2>&1

run_once() {
    "$PY" "$HERE/checkpoint.py" >>"$LOG" 2>&1
}

if [ "$SUPERVISE" -eq 0 ]; then
    run_once
    status=$?
    if [ "$status" -ne 0 ]; then
        tail_text="$(tail -n 12 "$LOG" 2>/dev/null)"
        notify "SafetyFirst stopped unexpectedly" \
               "The checkpoint exited with code $status.\n\nLast lines of $LOG:\n\n$tail_text"
    fi
    exit "$status"
fi

# Supervised: a checkpoint left running at a site gate should come back by
# itself. The backoff stops a permanently broken install from spinning -
# a config error would otherwise relaunch a few times a second all night.
delay=2
while true; do
    run_once
    status=$?
    [ "$status" -eq 0 ] && break
    printf '%s : exited %s, restarting in %ss\n' \
           "$(date '+%Y-%m-%d %H:%M:%S')" "$status" "$delay" >>"$LOG" 2>&1
    sleep "$delay"
    delay=$(( delay * 2 ))
    [ "$delay" -gt 60 ] && delay=60
done
