#!/bin/sh
# Installs TS_SmartCut for the current user (Linux).
#
#   sh install.sh
#
# - Puts the app in ${XDG_DATA_HOME:-~/.local/share}/TS_SmartCut (no root). The app keeps its settings,
#   history DB and logs next to the executable, so the folder must be the user's own.
# - Adds a menu entry (~/.local/share/applications/ts_smartcut.desktop).
# - Updating uses the same command: only the program files are replaced; settings.json, cut_history.db,
#   logs/, Logo/ and so on stay.
# - ffmpeg, ffprobe and libmpv are not bundled; install them with the distribution's packages.
set -eu

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your own user, not as root (TS_SmartCut is installed per user)." >&2
    exit 1
fi

SRC=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
DEST="$DATA_HOME/TS_SmartCut"
APPS="$DATA_HOME/applications"
DESKTOP="$APPS/ts_smartcut.desktop"

fail() {
    echo "Error: $*" >&2
    exit 1
}

[ -x "$SRC/app/TS_SmartCut" ] || fail "$SRC/app/TS_SmartCut is missing (run this in the folder the archive was extracted to)"
[ ! -L "$DEST" ] || fail "$DEST is a symbolic link"

echo "== Installing TS_SmartCut into $DEST =="
mkdir -p "$DEST"
# Program files only: the old _internal goes first so files the new version no longer has don't stay
rm -rf "$DEST/_internal.new"
cp -R "$SRC/app/_internal" "$DEST/_internal.new"
rm -rf "$DEST/_internal"
mv "$DEST/_internal.new" "$DEST/_internal"
for f in "$SRC/app/"*; do
    name=$(basename "$f")
    [ "$name" = "_internal" ] && continue
    cp -R "$f" "$DEST/$name"
done
chmod 755 "$DEST/TS_SmartCut"
# Qt's GTK3 look (GNOME and other GTK desktops: the look and the file dialogs follow GTK). It uses the PC's own
# GTK3, so it is added only when GTK3 is installed (the app works without it)
THEMES="$DEST/_internal/PySide6/Qt/plugins/platformthemes"
if [ -f "$SRC/optional/libqgtk3.so" ] && ldconfig -p 2>/dev/null | grep -q 'libgtk-3\.so\.0'; then
    mkdir -p "$THEMES"
    cp "$SRC/optional/libqgtk3.so" "$THEMES/libqgtk3.so"
    echo "GTK3 found: added the GTK3 look"
fi
cp "$SRC/uninstall.sh" "$DEST/uninstall.sh"
chmod 755 "$DEST/uninstall.sh"

mkdir -p "$APPS"
sed "s|@DEST@|$DEST|g" "$SRC/share/ts_smartcut.desktop" > "$DESKTOP"
chmod 644 "$DESKTOP"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi

echo "== Installed =="
echo "Start it from the application menu (TS_SmartCut) or with: $DEST/TS_SmartCut"
echo "Uninstall: sh $DEST/uninstall.sh"

missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
command -v ffprobe >/dev/null 2>&1 || missing="$missing ffprobe"
if ! { ldconfig -p 2>/dev/null | grep -q 'libmpv\.so'; }; then
    missing="$missing libmpv"
fi
if [ -n "$missing" ]; then
    echo
    echo "Not found:$missing"
    echo "TS_SmartCut needs ffmpeg/ffprobe (cutting and reading) and libmpv (the preview)."
    echo "Install them with your distribution's packages (for example: ffmpeg, mpv or libmpv2)."
fi
