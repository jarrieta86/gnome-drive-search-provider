# gnome-drive-search-provider

Search your Google Drive from the GNOME Activities overview.

Type part of a file name in the overview and a **Google Drive** section appears
with matching files from every Google account you have logged in to, personal
and work alike. Pick one and it opens in your browser with the right account.
Nothing is synced or indexed locally: each search is a live query to the Drive
API.

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
- An OAuth client of your own from Google Cloud, a one-time, free, five-minute
  setup (see [Connect your Google accounts](#connect-your-google-accounts)).

No other dependencies: the provider talks to D-Bus through GLib and to the
Drive API with the standard library.

## Install

```sh
git clone https://github.com/jarrieta86/gnome-drive-search-provider.git
cd gnome-drive-search-provider
./install.sh                  # current user
sudo ./install.sh --system    # or: every user, under /usr/local
```

Next, [connect your Google accounts](#connect-your-google-accounts). Then open the
Activities overview and type. GNOME Shell reloads its providers
when the desktop file is installed, so no restart is needed. If the **Google
Drive** section does not appear, check that it is enabled in **Settings >
Search**, or log out and back in.

**About per-user installs.** GNOME Shell only loads search provider definitions
from the system data directories listed in `XDG_DATA_DIRS`; it never reads
`~/.local/share/gnome-shell/search-providers`. `./install.sh` therefore places
the one-line `.ini` definition in the first entry of `XDG_DATA_DIRS` that you
can write to. With Flatpak installed that is
`~/.local/share/flatpak/exports/share`, the same place Flatpak uses to export
the search providers of its apps, and no root is needed. If no entry is
writable, the installer prints the single `sudo install` command that registers
the provider; everything else still lives in your home directory.

A **Google Drive** launcher is also added to the app grid (it opens Drive in the
browser). It has to be visible: GNOME Shell ignores search providers whose
desktop file is hidden with `NoDisplay=true`.

Remove everything with `./uninstall.sh` (or `sudo ./uninstall.sh --system`).

## How it works

`gnome-drive-search-provider` is a small D-Bus service started on demand by the
session bus the first time GNOME Shell asks it for results, and it exits after
five minutes without searches. It implements the five methods of
`org.gnome.Shell.SearchProvider2`:

- `GetInitialResultSet` / `GetSubsearchResultSet`: wait 250 ms after the last
  keystroke (so intermediate queries are skipped), then call `files.list` on the
  Drive API with `name contains '<term>'` for each term, across My Drive and
  shared drives, for all accounts in parallel, newest first.
- `GetResultMetas`: file name, a description with type, owner and date, and a
  themed icon matching the file type.
- `ActivateResult`: opens the file's web link with the default browser, as the
  account that found it.
- `LaunchSearch`: opens the same query in the Drive web search.

Queries shorter than 3 characters are ignored to avoid hammering the API.

## Connect your Google accounts

```sh
gnome-drive-search-provider --login --client-secret ~/Downloads/client_secret.json
```

Your browser opens on Google's consent screen; approve it and the account is
connected. The provider only asks for `drive.metadata.readonly`: it can see file
names, owners and dates, never file contents.

**Several accounts.** Run `--login` once per account (the client secret is only
needed the first time; it is remembered). All accounts are searched at the same
time, each result says which account it came from, and opening it adds
`authuser=<email>` to the link so the browser uses that account instead of your
default one. If your accounts live in different browser profiles, copy the link
that `--login` prints into the right profile instead of using the window it
opens.

```sh
gnome-drive-search-provider --login             # add another account
gnome-drive-search-provider --accounts          # list them
gnome-drive-search-provider --logout me@x.com   # revoke and remove one
```

Tokens are stored one file per account in
`~/.config/gnome-drive-search-provider/accounts/`, readable only by you. A
running provider notices new accounts within a minute.

### Creating the OAuth client

Google classifies every Drive scope as *restricted*, which means a project like
this cannot ship a shared, verified OAuth client without a paid yearly security
audit. So you create your own, once:

1. Open the [Google Cloud console](https://console.cloud.google.com/), create a
   project (any name) and enable the **Google Drive API** under *APIs & Services
   > Library*.
2. Under *APIs & Services > OAuth consent screen*, choose **External** (or
   **Internal** if this is a Google Workspace project and you only need accounts
   of that organization). Fill in the app name and your email.
3. Still on the consent screen, either add each of your accounts as a **test
   user**, or press **Publish app**. Prefer publishing: while an External app is
   in *Testing*, Google expires its refresh tokens after 7 days and you would
   have to `--login` again every week. A published, unverified app works for up
   to 100 users; you will see a "Google hasn't verified this app" warning during
   login, which is expected for your own client (*Advanced > Go to ...*).
4. Under *APIs & Services > Credentials*, create an **OAuth client ID** of type
   **Desktop app** and download its JSON. That file is the `--client-secret`.

Work accounts: a Google Workspace administrator can block third-party apps from
Drive. If login fails with "access blocked by your organization", ask the admin
to trust your client ID, or create the client inside the organization's own
Google Cloud as an *Internal* app.

### Why not GNOME Online Accounts?

It would be the natural choice, but it no longer works: current releases of
GNOME Online Accounts (checked on 3.58) do not request any Drive scope from
Google, so the tokens they hand out are rejected by the Drive API with
"insufficient authentication scopes". The *files* label in Settings is a
leftover. The provider still looks at GNOME Online Accounts for the benefit of
old GNOME releases, and silently stops using an account after the first such
rejection.

### Using an existing token file

If you already have a token in google-auth's `authorized_user` JSON format
(`client_id`, `client_secret`, `refresh_token`) with a Drive scope, you can point
the provider at it instead of logging in, with `auth.token_file` in the config
file or the `GNOME_DRIVE_SEARCH_TOKEN_FILE` environment variable.

## Configuration

Optional, in `~/.config/gnome-drive-search-provider/config.ini`. See
[`conf/config.ini.example`](conf/config.ini.example) for every key and its
default. The most useful ones:

| Key | Default | Meaning |
| --- | --- | --- |
| `search.mode` | `name` | `name` matches file names; `fulltext` also matches contents (slower, unsorted; log in with `--login --fulltext` to grant read access) |
| `search.max_results` | `10` | Results shown per search |
| `search.min_chars` | `3` | Shorter queries are ignored |
| `search.shared_drives` | `true` | Include shared drives |
| `auth.token_file` | empty | Extra account from an existing token file, see above |

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

No results: check `gnome-drive-search-provider --accounts`. An empty list means
you still have to `--login`. If an account is listed but the log says its token
"lacks the Drive permission", log in to it again (with `--fulltext` if you use
`mode = fulltext`). If searches stop working after a week, your OAuth app is
still in *Testing*; publish it (see above) and log in once more.

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
