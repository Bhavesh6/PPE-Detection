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
# Deliberately NOT locked here. checkpoint.py takes the lock itself, on this
# same file, for exactly as long as it runs.
#
# Locking in the launcher was wrong twice over. It guarded only launches
# that went through the launcher, so the desktop icon, a terminal and an
# ssh session could each start a second gate - which happened on the real
# device, two copies fighting over the master's serial port and logging
# "master disconnected" at each other while badges went unread. And it
# would now actively break startup: the launcher holding an exclusive lock
# on this path is precisely what the app's own claim would fail against.
#
# The message the app prints when it refuses reaches the log below, and
# the crash handler surfaces it.

# -- find the screen -------------------------------------------------------
# Tapping the icon inherits DISPLAY and XAUTHORITY from the desktop session,
# so nothing here is needed for that path. Every other way of starting the
# gate - a systemd unit, autostart, ssh, cron - inherits neither, and tkinter
# fails with "couldn't connect to display" no matter how healthy everything
# else is. That is what the old systemd unit did: it hardcoded DISPLAY=:0,
# set no XAUTHORITY, crashed, and restarted forever.
#
# XAUTHORITY cannot be hardcoded either. Under Wayland the Xwayland cookie
# is /run/user/<uid>/.mutter-Xwaylandauth.XXXXXX, and those six characters
# are new on every login - a path that works today is wrong tomorrow. So it
# is discovered, newest first, rather than written down.
: "${DISPLAY:=:0}"
export DISPLAY

if [ -z "${XAUTHORITY:-}" ]; then
    for candidate in \
        "/run/user/$(id -u)"/.mutter-Xwaylandauth.* \
        "/run/user/$(id -u)"/Xauthority \
        "$HOME/.Xauthority"
    do
        if [ -f "$candidate" ]; then
            XAUTHORITY="$candidate"
            export XAUTHORITY
            break
        fi
    done
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
    # Unbuffered, because the log is redirected to a file and Python then
    # block-buffers stdout: the gate would print its startup lines into a
    # 8KB buffer and the log would sit empty for minutes. A launcher whose
    # whole job is surfacing failures cannot have its evidence arrive late.
    PYTHONUNBUFFERED=1 "$PY" "$HERE/checkpoint.py" >>"$LOG" 2>&1
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
