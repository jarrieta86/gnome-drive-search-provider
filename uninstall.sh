#!/usr/bin/env bash
# Removes what install.sh installed. Use --system for a system-wide install.
set -euo pipefail

ID=io.github.jarrieta86.DriveSearchProvider
if [[ "${1:-}" == "--system" ]]; then
  PREFIX=${PREFIX:-/usr}
  DATADIR=${DATADIR:-$PREFIX/share}
  LIBEXECDIR=${LIBEXECDIR:-$PREFIX/libexec}
else
  DATADIR=${DATADIR:-${XDG_DATA_HOME:-$HOME/.local/share}}
  LIBEXECDIR=${LIBEXECDIR:-$HOME/.local/bin}
fi

pkill -u "$(id -u)" -f "^python3? .*/gnome-drive-search-provider$" 2>/dev/null || true
rm -f "$LIBEXECDIR/gnome-drive-search-provider" \
      "$DATADIR/gnome-shell/search-providers/$ID.ini" \
      "$DATADIR/applications/$ID.desktop" \
      "$DATADIR/dbus-1/services/$ID.service"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATADIR/applications" 2>/dev/null || true
command -v busctl >/dev/null && busctl --user reload 2>/dev/null || true
echo "Removed. Your config in ~/.config/gnome-drive-search-provider was left in place."
