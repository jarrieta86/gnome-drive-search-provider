#!/usr/bin/env bash
# Removes what install.sh installed. Use --system for a system-wide install.
set -euo pipefail

ID=io.github.jarrieta86.DriveSearchProvider
if [[ "${1:-}" == "--system" ]]; then
  PREFIX=${PREFIX:-/usr/local}
  DATADIR=${DATADIR:-$PREFIX/share}
  LIBEXECDIR=${LIBEXECDIR:-$PREFIX/libexec}
else
  DATADIR=${DATADIR:-${XDG_DATA_HOME:-$HOME/.local/share}}
  LIBEXECDIR=${LIBEXECDIR:-$HOME/.local/bin}
fi

pkill -u "$(id -u)" -f "^python3? .*/gnome-drive-search-provider$" 2>/dev/null || true
rm -f "$LIBEXECDIR/gnome-drive-search-provider" \
      "$DATADIR/applications/$ID.desktop" \
      "$DATADIR/dbus-1/services/$ID.service"

# The .ini may live in any data dir (see install.sh); remove every copy we can.
IFS=: read -ra dirs <<< "${PROVIDERDIR:+$PROVIDERDIR/../..:}$DATADIR:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
for dir in "${dirs[@]}"; do
  ini="${dir%/}/gnome-shell/search-providers/$ID.ini"
  if [[ -e "$ini" ]]; then
    rm -f "$ini" 2>/dev/null || echo "Could not remove $ini (try: sudo rm $ini)"
  fi
done

command -v update-desktop-database >/dev/null && update-desktop-database "$DATADIR/applications" 2>/dev/null || true
command -v busctl >/dev/null && busctl --user reload 2>/dev/null || true
echo "Removed. Your config in ~/.config/gnome-drive-search-provider was left in place."
