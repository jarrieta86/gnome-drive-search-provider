#!/usr/bin/env bash
# Installs the providers for the current user (default) or system-wide (--system).
# Add --no-setup to skip the offer to run the guided setup at the end.
#
# GNOME Shell only loads search provider definitions (.ini) from the system data
# directories in XDG_DATA_DIRS, never from ~/.local/share. A per-user install
# therefore puts the .ini files in the first user-writable XDG_DATA_DIRS entry
# (there is one when Flatpak is installed) and asks for a system-wide install
# otherwise.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

ID=io.github.jarrieta86.GoogleWorkspaceSearch
SERVICES=(Drive Gmail Calendar Contacts)
BIN=gnome-google-workspace-search

if [[ " $* " == *" --system "* ]]; then
  PREFIX=${PREFIX:-/usr/local}
  DATADIR=${DATADIR:-$PREFIX/share}
  LIBEXECDIR=${LIBEXECDIR:-$PREFIX/libexec}
  PROVIDERDIR=${PROVIDERDIR:-$DATADIR/gnome-shell/search-providers}
else
  DATADIR=${DATADIR:-${XDG_DATA_HOME:-$HOME/.local/share}}
  LIBEXECDIR=${LIBEXECDIR:-$HOME/.local/bin}
  if [[ -z "${PROVIDERDIR:-}" ]]; then
    IFS=: read -ra dirs <<< "${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
    for dir in "${dirs[@]}"; do
      if [[ -n "$dir" && -d "$dir" && -w "$dir" ]]; then
        PROVIDERDIR="${dir%/}/gnome-shell/search-providers"
        break
      fi
    done
  fi
fi

# The project used to be called gnome-drive-search-provider: replace that install.
OLD_ID=io.github.jarrieta86.DriveSearchProvider
pkill -u "$(id -u)" -f "^python3? .*/gnome-drive-search-provider$" 2>/dev/null || true
rm -f "$LIBEXECDIR/gnome-drive-search-provider" "$DATADIR/applications/$OLD_ID.desktop" \
      "$DATADIR/dbus-1/services/$OLD_ID.service"
[[ -n "${PROVIDERDIR:-}" ]] && rm -f "$PROVIDERDIR/$OLD_ID.ini"

install -Dm 0755 "$BIN.py" "$LIBEXECDIR/$BIN"
install -d "$DATADIR/dbus-1/services"
sed "s|@LIBEXECDIR@|$LIBEXECDIR|" "conf/$ID.service.in" > "$DATADIR/dbus-1/services/$ID.service"
for service in "${SERVICES[@]}"; do
  install -Dm 0644 "conf/$ID.$service.desktop" "$DATADIR/applications/$ID.$service.desktop"
  install -Dm 0644 "icons/$ID.$service.svg" "$DATADIR/icons/hicolor/scalable/apps/$ID.$service.svg"
  if [[ -n "${PROVIDERDIR:-}" ]]; then
    install -Dm 0644 "conf/$ID.$service.ini" "$PROVIDERDIR/$ID.$service.ini"
  fi
done

command -v update-desktop-database >/dev/null && update-desktop-database "$DATADIR/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$DATADIR/icons/hicolor" 2>/dev/null || true
# Ask the running session bus to pick up the new service file.
command -v busctl >/dev/null && busctl --user reload 2>/dev/null || true
# Stop a previous instance so the new code is used on the next search.
pkill -u "$(id -u)" -f "^python3? .*/$BIN$" 2>/dev/null || true

echo "Installed:"
echo "  $LIBEXECDIR/$BIN"
echo "  $DATADIR/dbus-1/services/$ID.service"
echo "  $DATADIR/applications/$ID.{Drive,Gmail,Calendar,Contacts}.desktop (and their icons)"
if [[ -n "${PROVIDERDIR:-}" ]]; then
  echo "  $PROVIDERDIR/$ID.{Drive,Gmail,Calendar,Contacts}.ini"
  cat <<MSG

Next: choose what to search and connect your Google accounts with the guided setup:

  $LIBEXECDIR/$BIN --setup

Only Google Drive is searched until you enable more there. Then open the
Activities overview and type. GNOME Shell picks the providers up right away; if
a section does not appear, check Settings > Search, or log out and back in.
MSG
  # Offer the guided setup right away, but only to a person at a terminal and
  # never as root (accounts belong to the user, not to whoever ran sudo).
  if [[ " $* " != *" --no-setup "* && -t 0 && -t 1 && $EUID -ne 0 ]]; then
    read -r -p "Run the guided setup now? [Y/n] " reply
    if [[ ! "$reply" =~ ^[Nn] ]]; then
      exec "$LIBEXECDIR/$BIN" --setup
    fi
  fi
else
  cat <<MSG

ONE STEP LEFT. GNOME Shell only reads search provider definitions from system
directories, and none of the entries in XDG_DATA_DIRS is writable by you.
Register the providers with:

  sudo install -Dm 0644 -t /usr/local/share/gnome-shell/search-providers "$PWD"/conf/$ID.*.ini

or install everything system-wide with: sudo ./install.sh --system
MSG
fi
