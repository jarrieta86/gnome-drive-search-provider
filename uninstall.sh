#!/usr/bin/env bash
# Removes what install.sh installed. Use --system for a system-wide install.
set -euo pipefail

ID=io.github.jarrieta86.GoogleWorkspaceSearch
SERVICES=(Drive Gmail Calendar Contacts)
BIN=gnome-google-workspace-search

if [[ " $* " == *" --system "* ]]; then
  PREFIX=${PREFIX:-/usr/local}
  DATADIR=${DATADIR:-$PREFIX/share}
  LIBEXECDIR=${LIBEXECDIR:-$PREFIX/libexec}
else
  DATADIR=${DATADIR:-${XDG_DATA_HOME:-$HOME/.local/share}}
  LIBEXECDIR=${LIBEXECDIR:-$HOME/.local/bin}
fi

pkill -u "$(id -u)" -f "^python3? .*/$BIN$" 2>/dev/null || true
rm -f "$LIBEXECDIR/$BIN" "$DATADIR/dbus-1/services/$ID.service"
rm -rf "$DATADIR/$BIN"

# The .ini files may live in any data dir (see install.sh); remove every copy we can.
IFS=: read -ra dirs <<< "${PROVIDERDIR:+$PROVIDERDIR/../..:}$DATADIR:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
for service in "${SERVICES[@]}"; do
  rm -f "$DATADIR/applications/$ID.$service.desktop" \
        "$DATADIR/icons/hicolor/scalable/apps/$ID.$service.svg"
  for dir in "${dirs[@]}"; do
    ini="${dir%/}/gnome-shell/search-providers/$ID.$service.ini"
    if [[ -e "$ini" ]]; then
      rm -f "$ini" 2>/dev/null || echo "Could not remove $ini (try: sudo rm $ini)"
    fi
  done
done

command -v update-desktop-database >/dev/null && update-desktop-database "$DATADIR/applications" 2>/dev/null || true
command -v busctl >/dev/null && busctl --user reload 2>/dev/null || true
echo "Removed. Your accounts and settings in ~/.config/gnome-google-workspace-search were left in place."
