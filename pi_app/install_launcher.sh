#!/usr/bin/env bash
#
# Puts SafetyFirst on the Pi's home screen.
#
#   ./install_launcher.sh              icon on the desktop + in the app menu
#   ./install_launcher.sh --autostart  also start it at login, supervised
#   ./install_launcher.sh --remove     take all of it back off
#
# Run as the desktop user, NOT with sudo: everything lands under $HOME, and
# a sudo run would install the launcher into root's home where the person
# using the Pi will never see it.

set -eu

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
TEMPLATE="$HERE/safetyfirst.desktop"

APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
ENTRY="safetyfirst.desktop"

# Raspberry Pi OS ships localised desktop directory names, so ~/Desktop is
# a guess. xdg-user-dir knows the real one; fall back if it is absent.
if command -v xdg-user-dir >/dev/null 2>&1; then
    DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
else
    DESKTOP_DIR="$HOME/Desktop"
fi

if [ "${1:-}" = "--remove" ]; then
    rm -f "$APPS/$ENTRY" "$DESKTOP_DIR/$ENTRY" "$AUTOSTART/$ENTRY"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$APPS" 2>/dev/null || true
    echo "Removed the SafetyFirst launcher, desktop icon and autostart entry."
    exit 0
fi

if [ "$(id -u)" = "0" ]; then
    echo "Run this as the desktop user, not with sudo - the icon would go to root's home." >&2
    exit 1
fi

[ -f "$TEMPLATE" ] || { echo "Missing $TEMPLATE" >&2; exit 1; }

chmod +x "$HERE/launch.sh" "$HERE/launch-doctor.sh" 2>/dev/null || true

# The template carries a placeholder because a .desktop file cannot resolve
# a relative path or expand $HOME in Exec.
write_entry() {
    dest="$1"; extra_exec="${2:-}"
    mkdir -p "$(dirname "$dest")"
    sed "s|__SAFETYFIRST_DIR__|$HERE|g" "$TEMPLATE" > "$dest"
    if [ -n "$extra_exec" ]; then
        # 0,/re/ so only the FIRST Exec= is rewritten - the ones inside the
        # [Desktop Action ...] blocks below it must keep their own commands.
        sed -i "0,/^Exec=/s|^Exec=.*|Exec=$HERE/launch.sh $extra_exec|" "$dest"
    fi
    chmod +x "$dest"
}

# -- app menu --------------------------------------------------------------
write_entry "$APPS/$ENTRY"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS" 2>/dev/null || true
echo "Added to the applications menu."

# -- desktop icon ----------------------------------------------------------
if [ -d "$DESKTOP_DIR" ]; then
    write_entry "$DESKTOP_DIR/$ENTRY"
    # File managers refuse to run a .desktop they do not trust. pcmanfm (the
    # Pi default) is satisfied by the executable bit above; GNOME's Files
    # wants this flag as well, and setting it on a system without gio is a
    # harmless no-op.
    command -v gio >/dev/null 2>&1 && \
        gio set "$DESKTOP_DIR/$ENTRY" metadata::trusted true 2>/dev/null || true
    echo "Put an icon on the desktop ($DESKTOP_DIR)."
else
    echo "No desktop folder found at $DESKTOP_DIR - menu entry only."
fi

# -- start at login --------------------------------------------------------
if [ "${1:-}" = "--autostart" ]; then
    write_entry "$AUTOSTART/$ENTRY" "--supervise"
    echo "Will start automatically at login, and restart itself if it crashes."
else
    echo
    echo "To also start it at login:  ./install_launcher.sh --autostart"
fi

echo
echo "Tap the hard-hat icon to open the gate. It opens fullscreen;"
echo "F11 leaves fullscreen, Esc closes it."
