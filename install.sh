#!/usr/bin/env bash
# Installs the provider for the current user (default) or system-wide (--system).
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

ID=io.github.jarrieta86.DriveSearchProvider
if [[ "${1:-}" == "--system" ]]; then
  PREFIX=${PREFIX:-/usr}
  DATADIR=${DATADIR:-$PREFIX/share}
  LIBEXECDIR=${LIBEXECDIR:-$PREFIX/libexec}
else
  DATADIR=${DATADIR:-${XDG_DATA_HOME:-$HOME/.local/share}}
  LIBEXECDIR=${LIBEXECDIR:-$HOME/.local/bin}
fi

install -Dm 0755 gnome-drive-search-provider.py "$LIBEXECDIR/gnome-drive-search-provider"
install -Dm 0644 "conf/$ID.ini" "$DATADIR/gnome-shell/search-providers/$ID.ini"
install -Dm 0644 "conf/$ID.desktop" "$DATADIR/applications/$ID.desktop"
install -d "$DATADIR/dbus-1/services"
sed "s|@LIBEXECDIR@|$LIBEXECDIR|" "conf/$ID.service.in" > "$DATADIR/dbus-1/services/$ID.service"

command -v update-desktop-database >/dev/null && update-desktop-database "$DATADIR/applications" 2>/dev/null || true
# Ask the running session bus to pick up the new service file.
command -v busctl >/dev/null && busctl --user reload 2>/dev/null || true
# Stop a previous instance so the new code is used on the next search.
pkill -u "$(id -u)" -f "^python3? .*/gnome-drive-search-provider$" 2>/dev/null || true

cat <<MSG
Installed:
  $LIBEXECDIR/gnome-drive-search-provider
  $DATADIR/gnome-shell/search-providers/$ID.ini
  $DATADIR/applications/$ID.desktop
  $DATADIR/dbus-1/services/$ID.service

Next: add your Google account in Settings > Online Accounts (with Files enabled),
then open the Activities overview and type part of a file name.
If the "Google Drive" section does not appear, log out and back in.
MSG
