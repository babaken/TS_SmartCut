#!/bin/sh
# Uninstalls TS_SmartCut for the current user (Linux).
#
#   sh ~/.local/share/TS_SmartCut/uninstall.sh          asks whether to delete the settings and data (default: keep)
#   sh ~/.local/share/TS_SmartCut/uninstall.sh --purge  deletes them without asking
set -eu

DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
DEST="$DATA_HOME/TS_SmartCut"
DESKTOP="$DATA_HOME/applications/ts_smartcut.desktop"
PURGE=no
[ "${1:-}" = "--purge" ] && PURGE=yes

[ ! -L "$DEST" ] || { echo "Error: $DEST is a symbolic link" >&2; exit 1; }

rm -f "$DESKTOP"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
fi
if [ -d "$DEST" ]; then
    rm -rf "$DEST/_internal" "$DEST/TS_SmartCut" "$DEST/README.md" "$DEST/README_jp.md" "$DEST/LICENSE" \
        "$DEST/THIRD_PARTY_NOTICES.md"
fi
echo "Removed TS_SmartCut"

if [ "$PURGE" = "no" ] && [ -t 0 ]; then
    printf "Also delete the settings, the cut history (cut_history.db), the logs, the caches and the Logo folder in %s? [y/N] " "$DEST"
    read -r answer
    case "$answer" in
        y|Y|yes|YES) PURGE=yes ;;
    esac
fi
if [ "$PURGE" = "yes" ]; then
    rm -rf "$DEST"
    echo "Deleted $DEST"
elif [ -n "$(ls -A "$DEST" 2>/dev/null | grep -vx uninstall.sh)" ]; then
    # Keep this script so the settings and data can still be deleted later
    echo "Kept the settings and data in $DEST"
    echo "To delete them later: sh $DEST/uninstall.sh --purge"
else
    rm -rf "$DEST"
fi
