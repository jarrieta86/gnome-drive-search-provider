# gnome-google-workspace-search

Search Google Drive, Gmail, Google Calendar and Google Contacts from the GNOME
Activities overview.

Type in the overview and each service you enabled shows its own section with
matches from every Google account you connected, personal and work alike. Pick a
result and it opens in your browser as the right account, in the right browser
profile. Nothing is synced or indexed locally: every search is a live query to
Google's APIs.

```
Activities  >  "budget"

  Google Drive
    Budget 2026 - draft          Spreadsheet - Ann Lee - 2026-09-01 - me@work.com
    budget-notes.docx            Word - Bob Ruiz - 2026-08-14 - me@gmail.com
  Gmail
    Re: Budget approval          Ann Lee - 2026-09-18 - me@work.com
  Google Calendar
    Budget review                2026-09-25 15:30 - Room 1 - me@work.com
  Google Contacts
    Ann Lee                      ann@work.com - CFO, Acme - me@work.com
```

Google Drive and Google Contacts are searched out of the box. Gmail and Google
Calendar stay off until you switch them on, and an account is only ever asked
for the permissions of the services you enabled.

> Formerly `gnome-drive-search-provider`. Installing this version replaces the
> old one and keeps your accounts and settings.

## Requirements

- GNOME Shell 3.36 or newer (any version with `org.gnome.Shell.SearchProvider2`).
- Python 3.8+ with PyGObject (`python3-gi` on Debian/Ubuntu, `python-gobject` on
  Arch, `python3-gobject` on Fedora). Already present on any GNOME desktop.
- An OAuth client of your own from Google Cloud, a one-time, free, five-minute
  step that the setup walks you through
  (see [Creating the OAuth client](#creating-the-oauth-client)).

No other dependencies: D-Bus through GLib, Google's APIs with the standard library.

## Install

```sh
git clone https://github.com/jarrieta86/gnome-google-workspace-search.git
cd gnome-google-workspace-search
./install.sh                  # current user
sudo ./install.sh --system    # or: every user, under /usr/local
```

Accept the installer's offer to run the guided setup, then open the Activities
overview and type. GNOME Shell reloads its providers when the desktop files are
installed, so no restart is needed. If a section does not appear, check that it
is enabled in **Settings > Search**, or log out and back in.

**About per-user installs.** GNOME Shell only loads search provider definitions
from the system data directories listed in `XDG_DATA_DIRS`; it never reads
`~/.local/share/gnome-shell/search-providers`. `./install.sh` therefore places
the one-line `.ini` definitions in the first entry of `XDG_DATA_DIRS` that you
can write to. With Flatpak installed that is
`~/.local/share/flatpak/exports/share`, the same place Flatpak uses to export
the search providers of its apps, and no root is needed. If no entry is
writable, the installer prints the single `sudo install` command that registers
the providers; everything else still lives in your home directory.

Four launchers (Google Drive, Gmail, Google Calendar, Google Contacts) are added
to the app grid; they open each service in the browser. They have to be visible:
GNOME Shell ignores search providers whose desktop file is hidden with
`NoDisplay=true`. The icons are the project's own, not Google's logos.

Remove everything with `./uninstall.sh` (or `sudo ./uninstall.sh --system`).

## Setup

```sh
gnome-google-workspace-search --setup
```

A short conversation in the terminal, safe to run again whenever you want to
change something. It never removes anything.

1. **GNOME Shell integration**: confirms the Shell can see the providers.
2. **Services**: a checklist of what to search, each entry with the permission
   it needs. Arrows (or `j`/`k`) move, space or `x` marks, `a` marks all, Enter
   confirms:

   ```
       [x] Google Drive     file names, owners and dates
       [x] Google Contacts  reads contacts, people you wrote to, work directory
     > [ ] Gmail            needs to read all your mail
       [ ] Google Calendar  reads your events

       Google has no permission to search mail without being able to read it, so
       this covers all your mail. Read-only; nothing is stored.
   ```

   The lines under the list explain the highlighted service and change as you
   move.

   Terminals that cannot be driven key by key get one yes/no question per
   service instead.
3. **OAuth client**: if you have none, walks you through registering one,
   opening each Google Cloud page for you
   (see [Creating the OAuth client](#creating-the-oauth-client)).
4. **Google accounts**: lists the connected accounts with the browser profile
   each opens in, asks already connected accounts to authorize any service you
   just enabled, and adds as many accounts as you want (one browser login each).
5. **Test**: runs a search across the enabled services.

### Services and permissions

| Service | Default | Permission requested | What it can see |
| --- | --- | --- | --- |
| Google Drive | on | `drive.metadata.readonly` | File names, owners, dates. Never contents, unless you opt in to content search (`drive.readonly`) |
| Google Contacts | on | `contacts.readonly`, `contacts.other.readonly`, `directory.readonly` | Your contacts, the people you have exchanged mail with and, on work accounts, your organization's directory |
| Gmail | off | `gmail.readonly` | **All your mail.** Google has a metadata-only permission, but it cannot search |
| Google Calendar | off | `calendar.events.readonly` | Events of your primary calendar |

All access is read-only. Tokens are stored one file per account in
`~/.config/gnome-google-workspace-search/accounts/`, readable only by you. Think
about who else can read your home directory before enabling Gmail.

A service can also be hidden at any time in **Settings > Search** without
touching its permission.

**Order of the sections.** GNOME Shell shows the providers listed in its
`sort-order` setting first and every other one alphabetically, which would put
Gmail above Google Drive. The setup therefore appends the four sections to that
setting in the project's order (Drive, Contacts, Gmail, Calendar), after whatever
you already had. It never rearranges existing entries, so an order you choose
later in **Settings > Search** sticks.

### Several accounts

Every account is searched at the same time, each result says which account it
came from, and opening it adds `authuser=<email>` to the link so the browser
uses that account instead of your default one. Accounts can differ in what they
authorized: a service simply skips the accounts that have not granted it.

**Browser profiles.** If your default browser is Chrome, Chromium, Brave, Edge
or Vivaldi and you keep each account in its own browser profile, results open in
the profile signed in to the account that found them. The provider reads the
browser's own profile list, so there is nothing to configure. If a profile is
not signed in to the browser itself (only to Google inside it), map it by hand:

```ini
# ~/.config/gnome-google-workspace-search/config.ini
[browser_profiles]
me@work.com = Profile 2
```

Profile directory names are shown in `chrome://version` under *Profile Path*.
Other browsers, and accounts with no matching profile, open in the default
browser window as usual. Set `use_profiles = false` under `[browser]` to turn
this off. When logging in, copy the link the setup prints into the right profile
instead of using the window it opens.

### Without the wizard

```sh
gnome-google-workspace-search --login --client-secret ~/Downloads/client_secret.json
gnome-google-workspace-search --login             # another account, or authorize one again
gnome-google-workspace-search --accounts          # accounts and what each can search
gnome-google-workspace-search --logout me@x.com   # revoke and remove one
```

`--login` requests the permissions of the services enabled in `config.ini`.

### Creating the OAuth client

Google only lets a program ask for access on behalf of an *OAuth client*
registered in a Google Cloud project. Programs that seem to need none (rclone,
Thunderbird, GNOME itself) simply ship theirs. This project cannot do that in
good conscience yet: Google classifies Drive and Gmail permissions as
*restricted*, and a shared client for them needs a paid yearly security audit,
or else is capped at 100 users. So you register your own, once. It is free,
takes about five minutes, and the client grants nothing by itself: it only
identifies the app. Access is granted later, per account, in your browser.

`--setup` walks you through it. It asks which Google account will own the
client (use a personal one: it can then accept any account), opens each of the
three pages in that account's browser profile, and picks up the file you
download at the end. It only ever offers a file downloaded during that walk,
never older ones lying in `~/Downloads`. The pages are:

1. **Project and APIs**: one link creates a project and enables the API of every
   service you chose (Google Drive API, Gmail API, Google Calendar API, People
   API). First-time Google Cloud users accept its terms there; no billing needed.
2. **Consent screen** (*Google Auth Platform > Get started*): any app name, it is
   what the login page will show. Audience **External**. Then, under *Audience*,
   press **Publish app**. Left in *Testing*, Google expires logins after 7 days
   and only accepts accounts listed as test users. A published, unverified app
   works for up to 100 users and shows a "Google hasn't verified this app"
   warning at login, which is expected for your own client
   (*Advanced > Go to ...*).
3. **OAuth client** (*Clients > Create*): application type **Desktop app**, then
   *Download JSON*.

Audience **Internal** is only offered inside a Google Workspace organization and
only accepts that organization's accounts. Work accounts: an administrator can
block third-party apps. If login fails with "access blocked by your
organization", ask the admin to trust your client ID, or create an Internal
client inside the organization's own Google Cloud.

**Shipping a client with a fork or package.** Put the client JSON at
`conf/oauth_client.json` before running `install.sh` and it is installed next to
the program and used whenever the user has not stored a client of their own, so
`--setup` goes straight to the browser login. Google does not treat the secret
of a desktop client as confidential, but mind the 100-user cap of unverified
apps and that every user shares your project's API quota.

### More than one OAuth client

Any Google account can own a client, personal or from an organization, and any
client can serve any account. One is usually enough. The exception is a client
whose audience is **Internal**: it only accepts accounts of its own Google
Workspace organization, and any other account ends on a Google error page
saying *"this client is restricted to users within its organization"*
(`org_internal`). Some organizations also block clients they do not own.

So the setup assumes nothing. When you add an account it asks for its email
first, which also lets the login open in that account's browser profile. With
several clients it asks which one to use, suggesting one created with an account
of the same domain, and lets you create a new one on the spot. If Google refuses
the account, press Ctrl+C and it offers the remaining clients or a new one.

```sh
gnome-google-workspace-search --new-client   # register one more, with any account
```

Each account remembers the client it was connected with, refreshes its token
with it, and is authorized again with it when you enable more services. Extra
clients are kept in `~/.config/gnome-google-workspace-search/clients/`, and the
default client is never replaced.

### Why not GNOME Online Accounts?

It would be the natural choice, but it no longer works: current releases of
GNOME Online Accounts (checked on 3.58) do not request any Drive scope from
Google, so the tokens they hand out are rejected with "insufficient
authentication scopes". The *files* label in Settings is a leftover. The
provider still looks at GNOME Online Accounts for the benefit of old GNOME
releases, and stops using an account for a service after the first rejection.

### Using an existing token file

If you already have a token in google-auth's `authorized_user` JSON format
(`client_id`, `client_secret`, `refresh_token`), you can point the provider at it
instead of logging in, with `auth.token_file` in the config file or the
`GNOME_DRIVE_SEARCH_TOKEN_FILE` environment variable. It is used for whichever
enabled services its scopes cover.

## Configuration

Optional, in `~/.config/gnome-google-workspace-search/config.ini`. The setup
writes the `[services]` section and `search.mode` for you. See
[`conf/config.ini.example`](conf/config.ini.example) for every key and its
default. The most useful ones:

| Key | Default | Meaning |
| --- | --- | --- |
| `services.drive`, `.contacts`, `.gmail`, `.calendar` | `true`, `true`, `false`, `false` | Which services are searched |
| `search.mode` | `name` | Drive only. `name` matches file names; `fulltext` also matches contents (slower, unsorted, needs read access) |
| `search.max_results` | `10` | Results per service |
| `search.min_chars` | `3` | Shorter queries are ignored |
| `search.shared_drives` | `true` | Include shared drives |
| `browser.use_profiles` | `true` | Open results in the browser profile of their account |
| `auth.token_file` | empty | Extra account from an existing token file, see above |

Descriptions are shown in English or Spanish depending on your locale.

Search tips: Gmail receives what you type untouched, so its operators work
(`from:ann has:attachment budget`). Calendar lists upcoming events first, then
the last 90 days. Contacts matches names, emails and phone numbers by prefix.

## How it works

`gnome-google-workspace-search` is one small D-Bus service, started on demand by
the session bus the first time GNOME Shell asks for results, which exits after
five minutes without searches. It exports one `org.gnome.Shell.SearchProvider2`
object per service, so GNOME Shell shows and manages each as a separate
provider:

- `GetInitialResultSet` / `GetSubsearchResultSet`: wait 250 ms after the last
  keystroke (so intermediate queries are skipped), then query the service's API
  for all accounts in parallel. A disabled service answers at once with nothing.
- `GetResultMetas`: title, a one-line description and an icon.
- `ActivateResult`: opens the result's web link with the default browser, as the
  account that found it and in that account's browser profile when there is one.
- `LaunchSearch`: opens the same query in the service's own web search.

Queries shorter than 3 characters are ignored to avoid hammering the APIs. When
Google refuses a service for an account in a way that will not fix itself
(missing permission, API not enabled in your project), the provider logs why
once and stops asking for that combination.

## Troubleshooting

Run a search from the terminal, without GNOME Shell involved:

```sh
gnome-google-workspace-search --query budget 2026
gnome-google-workspace-search --query budget --service gmail
```

Call the D-Bus service the way GNOME Shell does:

```sh
gdbus call --session --dest io.github.jarrieta86.GoogleWorkspaceSearch \
  --object-path /io/github/jarrieta86/GoogleWorkspaceSearch/Drive \
  --method org.gnome.Shell.SearchProvider2.GetInitialResultSet "['budget']"
```

Logs go to the journal: `journalctl --user -f | grep gnome-google-workspace-search`.
Start the service by hand with `gnome-google-workspace-search --verbose` to see
every request.

- **No results anywhere**: check `--accounts`. An empty list means you still have
  to run `--setup`.
- **One service shows nothing**: `--accounts` tells you which accounts are "not
  authorized for" it; run `--setup` to authorize them. If the log says the API
  "is not enabled in the Google Cloud project", enable it in the console (step 1
  of the client guide) and restart the provider.
- **Everything stops working after a week**: your OAuth app is still in
  *Testing*; publish it (see above) and log in once more.

## Development

```sh
python3 -m venv --system-site-packages .venv   # keeps access to PyGObject
.venv/bin/pip install pytest ruff
.venv/bin/pytest
.venv/bin/ruff check .
```

Tests do not touch the network or D-Bus. Adding a service means one `Service`
subclass (scopes, search, presentation, links) plus its `.ini`, `.desktop` and
icon; accounts, login, the setup and the D-Bus plumbing are shared.

## License

MIT. See [LICENSE](LICENSE).
