# gnome-drive-search-provider

Search your Google Drive from the GNOME Activities overview.

Type part of a file name in the overview and a **Google Drive** section appears
with matching files from every Google account you have configured in GNOME.
Pick one and it opens in your browser. Nothing is synced or indexed locally:
each search is a live query to the Drive API.

```
Activities  >  "budget 2026"

  Google Drive
    Budget 2026 - draft         Spreadsheet - Ann Lee - 2026-09-01
    Budget 2026 planning        Folder - Ann Lee - 2026-08-20
    budget-2026-notes.docx      Word - Bob Ruiz - 2026-08-14
```

## Requirements

- GNOME Shell 3.36 or newer (any version with `org.gnome.Shell.SearchProvider2`).
- Python 3.8+ with PyGObject (`python3-gi` on Debian/Ubuntu, `python-gobject` on
  Arch, `python3-gobject` on Fedora). Already present on any GNOME desktop.
- A Google account in **Settings > Online Accounts** with *Files* enabled.
  Alternatively, a refresh-token file (see [Authentication](#authentication)).

No other dependencies: the provider talks to D-Bus through GLib and to the
Drive API with the standard library.

## Install

For the current user (no root needed):

```sh
git clone https://github.com/jarrieta86/gnome-drive-search-provider.git
cd gnome-drive-search-provider
./install.sh
```

System-wide, for every user on the machine:

```sh
sudo ./install.sh --system
```

Then open the Activities overview and type. If the **Google Drive** section does
not appear, check that it is enabled in **Settings > Search**, and log out and
back in if you installed system-wide (GNOME Shell reloads user providers on the
fly, but system directories are scanned at login).

Remove it with `./uninstall.sh` (or `sudo ./uninstall.sh --system`).

## How it works

`gnome-drive-search-provider` is a small D-Bus service started on demand by the
session bus the first time GNOME Shell asks it for results, and it exits after
five minutes without searches. It implements the five methods of
`org.gnome.Shell.SearchProvider2`:

- `GetInitialResultSet` / `GetSubsearchResultSet`: wait 250 ms after the last
  keystroke (so intermediate queries are skipped), then call `files.list` on the
  Drive API with `name contains '<term>'` for each term, across My Drive and
  shared drives, ordered by modification date.
- `GetResultMetas`: file name, a description with type, owner and date, and a
  themed icon matching the file type.
- `ActivateResult`: opens the file's web link with the default browser.
- `LaunchSearch`: opens the same query in the Drive web search.

Queries shorter than 3 characters are ignored to avoid hammering the API.

## Authentication

**GNOME Online Accounts (default).** The provider asks GNOME Online Accounts for
an access token of every Google account that has *Files* enabled. Tokens are
refreshed by GNOME; the provider stores nothing. Accounts added or removed while
the provider is running are picked up within a minute.

**Token file (fallback).** If you cannot or do not want to use GNOME Online
Accounts, point the provider at a JSON file in the `authorized_user` format used
by google-auth, `gcloud`, and most Google CLI tools. It must contain
`client_id`, `client_secret` and `refresh_token`, and the token must have the
`drive.readonly` scope (or broader). The provider refreshes the access token and
writes it back to the same file.

```ini
# ~/.config/gnome-drive-search-provider/config.ini
[auth]
token_file = ~/.config/my-tool/google_token.json
```

The `GNOME_DRIVE_SEARCH_TOKEN_FILE` environment variable overrides this setting.
When both GNOME Online Accounts and a token file are available, all of them are
searched and each result shows which account it came from.

## Configuration

Optional, in `~/.config/gnome-drive-search-provider/config.ini`. See
[`conf/config.ini.example`](conf/config.ini.example) for every key and its
default. The most useful ones:

| Key | Default | Meaning |
| --- | --- | --- |
| `search.mode` | `name` | `name` matches file names; `fulltext` also matches contents (slower, unsorted) |
| `search.max_results` | `10` | Results shown per search |
| `search.min_chars` | `3` | Shorter queries are ignored |
| `search.shared_drives` | `true` | Include shared drives |
| `auth.token_file` | empty | Fallback token file, see above |

Descriptions are shown in English or Spanish depending on your locale.

## Troubleshooting

Run a search from the terminal, without GNOME Shell involved:

```sh
gnome-drive-search-provider --query budget 2026
```

Call the D-Bus service the way GNOME Shell does:

```sh
gdbus call --session --dest io.github.jarrieta86.DriveSearchProvider \
  --object-path /io/github/jarrieta86/DriveSearchProvider \
  --method org.gnome.Shell.SearchProvider2.GetInitialResultSet "['budget']"
```

Logs go to the journal: `journalctl --user -f | grep gnome-drive-search-provider`.
Start the service by hand with `gnome-drive-search-provider --verbose` to see
every request.

No Google account found: add one in **Settings > Online Accounts** and make sure
*Files* is switched on for it, or configure `auth.token_file`.

## Development

```sh
python3 -m venv --system-site-packages .venv   # keeps access to PyGObject
.venv/bin/pip install pytest ruff
.venv/bin/pytest
.venv/bin/ruff check .
```

Tests do not touch the network or D-Bus.

## License

MIT. See [LICENSE](LICENSE).
