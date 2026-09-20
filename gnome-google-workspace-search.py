#!/usr/bin/env python3
"""GNOME Shell search providers for Google Workspace.

One D-Bus service exports a search provider (org.gnome.Shell.SearchProvider2)
per Google service: Drive, Contacts, Gmail and Calendar. Each one shows up as its
own section in the Activities overview and can be switched on and off, both here
(``--setup``) and in Settings > Search. Drive and Contacts are enabled by default,
Gmail and Calendar are not, and an account is only ever asked for the permissions
of the services you enable.

Accounts are added with ``--setup`` or ``--login``, which run the OAuth flow in
your browser using your own OAuth client and store one token file per account
under ``~/.config/gnome-google-workspace-search/accounts``. Every account is
searched, in parallel. Two more account sources exist for compatibility:

- ``auth.token_file``: an existing "authorized_user" JSON token (google-auth).
- GNOME Online Accounts, on the old GNOME releases whose Google tokens still
  carry the needed scopes. Current releases do not, and such accounts are skipped.

The process is started on demand by D-Bus activation and exits after a period
of inactivity.
"""

import argparse
import base64
import concurrent.futures
import configparser
import contextlib
import email.utils
import hashlib
import http.server
import json
import locale
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

APP_ID = "io.github.jarrieta86.GoogleWorkspaceSearch"
BUS_NAME = APP_ID
OBJECT_PATH = "/" + APP_ID.replace(".", "/")

GOA_BUS_NAME = "org.gnome.OnlineAccounts"
GOA_OBJECT_PATH = "/org/gnome/OnlineAccounts"
GOA_ACCOUNT_IFACE = "org.gnome.OnlineAccounts.Account"
GOA_OAUTH2_IFACE = "org.gnome.OnlineAccounts.OAuth2Based"

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_FIELDS = "files(id,name,mimeType,webViewLink,modifiedTime,owners(displayName))"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
PEOPLE_URL = "https://people.googleapis.com/v1"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Least privilege: file names and metadata only. Full-text search needs read access.
SCOPE_METADATA = "https://www.googleapis.com/auth/drive.metadata.readonly"
SCOPE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.events.readonly"
SCOPE_CONTACTS = "https://www.googleapis.com/auth/contacts.readonly"
SCOPE_DIRECTORY = "https://www.googleapis.com/auth/directory.readonly"
SCOPE_OTHER_CONTACTS = "https://www.googleapis.com/auth/contacts.other.readonly"
# Lets us label the account without depending on any particular service.
SCOPE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"

LEGACY_CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "gnome-drive-search-provider")

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "gnome-google-workspace-search")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")
ACCOUNTS_DIR = os.path.join(CONFIG_DIR, "accounts")
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, "client_secret.json")
# Extra OAuth clients, for accounts the default one does not accept.
CLIENTS_DIR = os.path.join(CONFIG_DIR, "clients")
# A distribution of this project may ship its own OAuth client, the way rclone or
# Thunderbird do, so that its users never have to create one. Looked up next to the
# script (running from a checkout) and in the data directories (installed).
BUNDLED_CLIENT_NAME = "oauth_client.json"


def bundled_client_path():
    here = os.path.dirname(os.path.realpath(__file__))
    candidates = [os.path.join(here, "conf", BUNDLED_CLIENT_NAME)]
    for data_dir in [GLib.get_user_data_dir(), *GLib.get_system_data_dirs()]:
        candidates.append(os.path.join(data_dir, "gnome-google-workspace-search", BUNDLED_CLIENT_NAME))
    return next((c for c in candidates if os.path.exists(c)), None)


def default_client_path(store=None):
    """The client to log in with: the user's own if stored, else the bundled one."""
    store = store or CLIENT_SECRET_PATH
    if os.path.exists(store):
        return store
    return bundled_client_path() or store

# Chromium-family browsers keep one "Local State" file per installation that
# lists every profile and the Google account signed in to it. Keyed by the
# desktop file id of the browser; values are relative to the user's home.
CHROMIUM_BROWSERS = {
    "google-chrome.desktop": ".config/google-chrome",
    "google-chrome-beta.desktop": ".config/google-chrome-beta",
    "google-chrome-unstable.desktop": ".config/google-chrome-unstable",
    "chromium.desktop": ".config/chromium",
    "chromium-browser.desktop": ".config/chromium",
    "brave-browser.desktop": ".config/BraveSoftware/Brave-Browser",
    "microsoft-edge.desktop": ".config/microsoft-edge",
    "vivaldi-stable.desktop": ".config/vivaldi",
    "com.google.Chrome.desktop": ".var/app/com.google.Chrome/config/google-chrome",
    "org.chromium.Chromium.desktop": ".var/app/org.chromium.Chromium/config/chromium",
    "com.brave.Browser.desktop": ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
}

DEFAULTS = {
    "mode": "name",  # "name" matches file names, "fulltext" also matches content
    "max_results": 10,
    "min_chars": 3,
    "debounce_ms": 250,
    "shared_drives": True,
    "idle_exit_seconds": 300,
    "token_file": "",
    "services": {"drive": True, "contacts": True, "gmail": False, "calendar": False},
    "use_profiles": True,
    "profiles": {},  # manual overrides: account email -> browser profile directory
}

ICONS = {
    "application/vnd.google-apps.document": "x-office-document",
    "application/vnd.google-apps.spreadsheet": "x-office-spreadsheet",
    "application/vnd.google-apps.presentation": "x-office-presentation",
    "application/vnd.google-apps.form": "x-office-document",
    "application/vnd.google-apps.folder": "folder",
    "application/vnd.google-apps.drawing": "image-x-generic",
    "application/vnd.google-apps.shortcut": "emblem-symbolic-link",
    "application/pdf": "application-pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "x-office-document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "x-office-spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "x-office-presentation",
    "application/msword": "x-office-document",
    "application/vnd.ms-excel": "x-office-spreadsheet",
    "application/vnd.ms-powerpoint": "x-office-presentation",
    "text/plain": "text-x-generic",
    "text/csv": "x-office-spreadsheet",
}

KINDS = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.form": "form",
    "application/vnd.google-apps.folder": "folder",
    "application/vnd.google-apps.drawing": "drawing",
    "application/vnd.google-apps.shortcut": "shortcut",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "powerpoint",
    "application/msword": "word",
    "application/vnd.ms-excel": "excel",
    "application/vnd.ms-powerpoint": "powerpoint",
    "text/plain": "text",
    "text/csv": "csv",
}

LABELS = {
    "en": {
        "document": "Document", "spreadsheet": "Spreadsheet", "presentation": "Presentation",
        "form": "Form", "folder": "Folder", "drawing": "Drawing", "shortcut": "Shortcut",
        "pdf": "PDF", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint",
        "text": "Text", "csv": "CSV", "image": "Image", "video": "Video", "audio": "Audio",
        "file": "File",
        "no_subject": "(no subject)", "no_title": "(no title)",
    },
    "es": {
        "document": "Documento", "spreadsheet": "Hoja de cálculo", "presentation": "Presentación",
        "form": "Formulario", "folder": "Carpeta", "drawing": "Dibujo", "shortcut": "Acceso directo",
        "pdf": "PDF", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint",
        "text": "Texto", "csv": "CSV", "image": "Imagen", "video": "Video", "audio": "Audio",
        "file": "Archivo",
        "no_subject": "(sin asunto)", "no_title": "(sin título)",
    },
}

INTROSPECTION_XML = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""

VERBOSE = False


def log(msg, always=False):
    if VERBOSE or always:
        print(f"[gnome-google-workspace-search] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(path=CONFIG_PATH):
    """Read the config file, falling back to DEFAULTS for missing keys."""
    cfg = dict(DEFAULTS)
    parser = configparser.ConfigParser()
    if path and os.path.exists(path):
        parser.read(path)
    search = parser["search"] if parser.has_section("search") else {}
    auth = parser["auth"] if parser.has_section("auth") else {}

    def read(section, key, cast):
        if key in section:
            try:
                cfg[key] = cast(section[key])
            except ValueError:
                log(f"config: ignoring invalid value for {key}", always=True)

    def boolean(value):
        return value.strip().lower() in ("1", "true", "yes", "on")

    read(search, "mode", str)
    read(search, "max_results", int)
    read(search, "min_chars", int)
    read(search, "debounce_ms", int)
    read(search, "shared_drives", boolean)
    read(search, "idle_exit_seconds", int)
    read(auth, "token_file", str)
    if parser.has_section("services"):
        services = dict(cfg["services"])
        for key in services:
            if key in parser["services"]:
                services[key] = boolean(parser["services"][key])
        cfg["services"] = services
    else:
        cfg["services"] = dict(cfg["services"])
    browser = parser["browser"] if parser.has_section("browser") else {}
    read(browser, "use_profiles", boolean)
    if parser.has_section("browser_profiles"):
        cfg["profiles"] = {k.lower(): v.strip() for k, v in parser["browser_profiles"].items()}
    if cfg["mode"] not in ("name", "fulltext"):
        log(f"config: unknown mode {cfg['mode']!r}, using 'name'", always=True)
        cfg["mode"] = "name"
    env_token = os.environ.get("GNOME_DRIVE_SEARCH_TOKEN_FILE")
    if env_token:
        cfg["token_file"] = env_token
    cfg["token_file"] = os.path.expanduser(cfg["token_file"])
    return cfg


def ui_language():
    lang = os.environ.get("LANGUAGE") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG")
    if not lang:
        lang = (locale.getlocale()[0] or "en")
    code = lang.split(":")[0].split("_")[0].split(".")[0].lower()
    return code if code in LABELS else "en"


def kind_label(mime, lang):
    labels = LABELS[lang]
    key = KINDS.get(mime)
    if key is None:
        if mime.startswith("image/"):
            key = "image"
        elif mime.startswith("video/"):
            key = "video"
        elif mime.startswith("audio/"):
            key = "audio"
        else:
            key = "file"
    return labels[key]


# ---------------------------------------------------------------------------
# Authentication backends
# ---------------------------------------------------------------------------


class Account:
    """A Google account we can search, with a way to get a fresh access token."""

    def __init__(self, identity, token_getter, source="login", scopes=None):
        self.identity = identity
        self.source = source
        # Granted OAuth scopes when known (token files record them); None = unknown.
        self.scopes = set(scopes) if scopes else None
        # Services Google refused for this account, so we stop asking.
        self.disabled_services = set()
        self._token_getter = token_getter
        self._token = None
        self._expires_at = 0.0
        self._email = identity if "@" in identity else None
        self._email_resolved = self._email is not None

    @property
    def email(self):
        return self._email

    def resolve_email(self):
        """Ask Drive who this token belongs to (once), for accounts not named after it."""
        if not self._email_resolved:
            self._email_resolved = True
            try:
                self._email = fetch_email(self.token())
            except Exception as e:  # noqa: BLE001
                log(f"[{self.identity}] could not resolve the account email: {e}")
        return self._email

    def token(self, force=False):
        if force or self._token is None or time.monotonic() >= self._expires_at:
            self._token, expires_in = self._token_getter()
            self._expires_at = time.monotonic() + max(int(expires_in) - 60, 30)
        return self._token


def goa_accounts(bus=None):
    """Google accounts from GNOME Online Accounts with the Files feature enabled."""
    try:
        bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        manager = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None, GOA_BUS_NAME,
            GOA_OBJECT_PATH, "org.freedesktop.DBus.ObjectManager", None,
        )
        objects = manager.call_sync(
            "GetManagedObjects", None, Gio.DBusCallFlags.NONE, 5000, None
        ).unpack()[0]
    except GLib.Error as e:
        log(f"GNOME Online Accounts unavailable: {e.message}")
        return []

    accounts = []
    for path, ifaces in objects.items():
        props = ifaces.get(GOA_ACCOUNT_IFACE)
        if not props or props.get("ProviderType") != "google":
            continue
        if props.get("FilesDisabled") or GOA_OAUTH2_IFACE not in ifaces:
            continue
        identity = props.get("PresentationIdentity") or props.get("Identity") or path
        accounts.append(Account(identity, _goa_token_getter(bus, path), source="goa"))
    return accounts


def _goa_token_getter(bus, path):
    def get():
        account = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None, GOA_BUS_NAME,
            path, GOA_ACCOUNT_IFACE, None,
        )
        account.call_sync("EnsureCredentials", None, Gio.DBusCallFlags.NONE, 30000, None)
        oauth = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None, GOA_BUS_NAME,
            path, GOA_OAUTH2_IFACE, None,
        )
        token, expires_in = oauth.call_sync(
            "GetAccessToken", None, Gio.DBusCallFlags.NONE, 30000, None
        ).unpack()
        return token, expires_in

    return get


class TokenFile:
    """OAuth refresh-token file in google-auth "authorized_user" format."""

    def __init__(self, path, identity=None, source="token_file"):
        self.path = path
        self.identity = identity
        self.source = source
        self.lock = threading.Lock()
        self.data = None

    def account(self):
        if not self.path or not os.path.exists(self.path):
            return None
        identity = self.identity or os.path.basename(self.path)
        return Account(identity, self.fresh_token, source=self.source, scopes=self._scopes())

    def _scopes(self):
        try:
            with open(self.path) as f:
                scopes = json.load(f).get("scopes")
        except (OSError, ValueError):
            return None
        if isinstance(scopes, str):
            scopes = scopes.split()
        return scopes or None

    def _load(self):
        with open(self.path) as f:
            self.data = json.load(f)
        for key in ("client_id", "client_secret", "refresh_token"):
            if key not in self.data:
                raise ValueError(f"token file is missing {key!r}")

    def _expiry(self):
        expiry = self.data.get("expiry")
        if not expiry or not self.data.get("token"):
            return None
        text = expiry.rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def fresh_token(self):
        """Return (token, seconds_until_expiry), refreshing if needed."""
        with self.lock:
            if self.data is None:
                self._load()
            expiry = self._expiry()
            now = datetime.now(timezone.utc)
            if expiry is not None and expiry - now > timedelta(seconds=90):
                return self.data["token"], int((expiry - now).total_seconds())
            return self._refresh()

    def _refresh(self):
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.data["refresh_token"],
            "client_id": self.data["client_id"],
            "client_secret": self.data["client_secret"],
        }).encode()
        req = urllib.request.Request(self.data.get("token_uri") or TOKEN_URL, data=body)
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
        expires_in = int(payload.get("expires_in", 3600))
        self.data["token"] = payload["access_token"]
        exp = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        self.data["expiry"] = exp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        write_private_json(self.path, self.data)
        log("token file refreshed")
        return self.data["token"], expires_in


def write_private_json(path, data):
    """Atomically write JSON readable only by the user (or keep the file's mode)."""
    mode = 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Login: OAuth 2.0 for desktop apps (loopback redirect + PKCE)
# ---------------------------------------------------------------------------


class LoginError(Exception):
    pass


def load_client_secret(path):
    """Read a Google OAuth client JSON ("Desktop app" download)."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise LoginError(
            f"no OAuth client found at {path}. Pass --client-secret FILE the first "
            "time you log in (see the README for how to create one)."
        ) from None
    except ValueError as e:
        raise LoginError(f"{path} is not valid JSON: {e}") from None
    kind = "installed" if "installed" in raw else "web" if "web" in raw else None
    client = raw.get(kind, raw) if kind else raw
    for key in ("client_id", "client_secret"):
        if not client.get(key):
            raise LoginError(f"{path} has no {key!r}; download the OAuth client JSON again")
    return {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "token_uri": client.get("token_uri") or TOKEN_URL,
        "kind": kind or "installed",
        "project": client.get("project_id") or client["client_id"].split("-")[0],
    }


def remember_client(path, clients_dir=None):
    """Keep a copy of an extra OAuth client so it can be offered again."""
    clients_dir = clients_dir or CLIENTS_DIR
    client = load_client_secret(path)
    os.makedirs(clients_dir, mode=0o700, exist_ok=True)
    safe = "".join(c for c in client["project"] if c.isalnum() or c in "._-") or "client"
    target = os.path.join(clients_dir, safe + ".json")
    if os.path.abspath(path) != os.path.abspath(target):
        shutil.copyfile(path, target)
    os.chmod(target, 0o600)
    return target


def known_clients(store=None, clients_dir=None):
    """(client, path) of every stored OAuth client, the default one first."""
    store, clients_dir = store or CLIENT_SECRET_PATH, clients_dir or CLIENTS_DIR
    paths = [store] + ([bundled_client_path()] if bundled_client_path() else [])
    try:
        paths += sorted(os.path.join(clients_dir, n) for n in os.listdir(clients_dir)
                        if n.endswith(".json"))
    except FileNotFoundError:
        pass
    found, seen = [], set()
    for path in paths:
        try:
            client = load_client_secret(path)
        except LoginError:
            continue
        if client["client_id"] not in seen:
            seen.add(client["client_id"])
            found.append((client, path))
    return found


def client_of_account(email, accounts_dir=None):
    """The OAuth client an account was connected with; authorize it again with the same one."""
    try:
        with open(account_path(email, accounts_dir)) as f:
            data = json.load(f)
        return {
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "token_uri": data.get("token_uri") or TOKEN_URL,
            "kind": "installed",
            "project": data["client_id"].split("-")[0],
        }
    except (OSError, ValueError, KeyError):
        return None


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_auth_url(client, redirect_uri, scope, state, challenge, login_hint=None):
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        # Keep what the account already granted when enabling one more service.
        "include_granted_scopes": "true",
        # Always ask for consent so Google returns a refresh token every time.
        "prompt": "consent select_account",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise LoginError(f"Google rejected the request (HTTP {e.code}): {detail}") from None


def exchange_code(client, code, verifier, redirect_uri):
    payload = _post_form(client["token_uri"], {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri,
    })
    if not payload.get("refresh_token"):
        raise LoginError("Google did not return a refresh token; try --login again")
    return payload


def fetch_email(access_token):
    """Address of the account behind a token, asking whichever API it can use."""
    sources = (
        (DRIVE_ABOUT_URL, lambda d: d["user"]["emailAddress"]),
        (GMAIL_URL + "/profile", lambda d: d["emailAddress"]),
        (USERINFO_URL, lambda d: d["email"]),
    )
    detail = ""
    for url, pick in sources:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return pick(json.load(resp))
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: " + e.read()[:300].decode(errors="replace")
        except (KeyError, TypeError, ValueError):
            detail = f"unexpected answer from {url}"
    raise LoginError(
        "logged in, but Google would not say which account this is. Are the APIs of the "
        f"services you enabled switched on in your Google Cloud project? {detail}"
    )


def wait_for_redirect(server, state, timeout):
    """Serve the loopback redirect until Google sends us the code."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_error(404)  # favicon and friends
                return
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query["code"][0]
            ok = "code" in result
            body = (
                "<html><body style='font-family:sans-serif;margin:3em'><h2>"
                + ("Google Drive search: account connected" if ok else "Login failed")
                + "</h2><p>"
                + ("You can close this tab." if ok else result.get("error", ""))
                + "</p></body></html>"
            ).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        server.timeout = max(0.1, min(1.0, deadline - time.monotonic()))
        server.handle_request()
    if "code" in result:
        return result["code"]
    raise LoginError(result.get("error") or "timed out waiting for the browser")


def account_path(email, accounts_dir=None):
    safe = "".join(c for c in email.lower() if c.isalnum() or c in "@._-+")
    return os.path.join(accounts_dir or ACCOUNTS_DIR, safe + ".json")


def save_account(email, client, payload, scope, accounts_dir=None):
    accounts_dir = accounts_dir or ACCOUNTS_DIR
    os.makedirs(accounts_dir, mode=0o700, exist_ok=True)
    os.chmod(accounts_dir, 0o700)
    expires_in = int(payload.get("expires_in", 3600))
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    data = {
        "type": "authorized_user",
        "account": email,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "token_uri": client["token_uri"],
        "refresh_token": payload["refresh_token"],
        "token": payload["access_token"],
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "scopes": (payload.get("scope") or scope).split(),
    }
    path = account_path(email, accounts_dir)
    if os.path.exists(path):
        os.chmod(path, 0o600)
    write_private_json(path, data)
    return path


def stored_accounts(accounts_dir=None):
    """Accounts added with --login, one JSON file each, named after the email."""
    accounts_dir = accounts_dir or ACCOUNTS_DIR
    try:
        names = sorted(n for n in os.listdir(accounts_dir) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        token_file = TokenFile(os.path.join(accounts_dir, name), identity=name[:-5], source="login")
        found.append(token_file.account())
    return found


def login(client_secret=None, fulltext=False, port=0, open_browser=True, timeout=300,
          accounts_dir=None, client_secret_store=None, scopes=None, login_hint=None,
          client=None, clients_dir=None):
    """Add (or re-authorize) a Google account. Returns its email.

    scopes lists what to ask for; without it, Drive alone (contents too with fulltext).
    client_secret is an OAuth client file to use instead of the stored default; client
    is one already loaded (the one an existing account was connected with).
    """
    store = client_secret_store or CLIENT_SECRET_PATH
    client = client or load_client_secret(client_secret or default_client_path(store))
    scope = " ".join(scopes) if scopes else (SCOPE_READONLY if fulltext else SCOPE_METADATA)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)

    server = http.server.HTTPServer(("127.0.0.1", port), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = build_auth_url(client, redirect_uri, scope, state, challenge, login_hint)
        print("Open this link in the browser profile of the account you want to add:\n")
        print(f"  {url}\n")
        if client["kind"] == "web" and not port:
            print("Note: this is a 'Web application' OAuth client. Google will only accept the\n"
                  "redirect if it is registered; use --port with a registered port, or create a\n"
                  "'Desktop app' client instead.\n")
        if open_browser:
            try:
                Gio.AppInfo.launch_default_for_uri(url, None)
            except GLib.Error:
                pass
        print("Waiting for you to approve access... (if Google shows an error page instead, "
              "press Ctrl+C here)", flush=True)
        code = wait_for_redirect(server, state, timeout)
    finally:
        server.server_close()

    payload = exchange_code(client, code, verifier, redirect_uri)
    email = fetch_email(payload["access_token"])
    path = save_account(email, client, payload, scope, accounts_dir)
    if client_secret and os.path.abspath(client_secret) != os.path.abspath(store):
        if os.path.exists(store):
            # Never replace the default client: other accounts may depend on it.
            remember_client(client_secret, clients_dir or os.path.join(os.path.dirname(store), "clients"))
        else:
            os.makedirs(os.path.dirname(store), mode=0o700, exist_ok=True)
            shutil.copyfile(client_secret, store)
            os.chmod(store, 0o600)
    print(f"Connected {email} (token saved to {path}).")
    return email


def logout(email, accounts_dir=None):
    path = account_path(email, accounts_dir)
    if not os.path.exists(path):
        raise LoginError(f"no account {email!r}; see --accounts")
    try:
        with open(path) as f:
            refresh_token = json.load(f).get("refresh_token")
        if refresh_token:
            _post_form(REVOKE_URL, {"token": refresh_token})
    except (LoginError, OSError, ValueError) as e:
        log(f"could not revoke the token at Google: {e}", always=True)
    os.remove(path)
    print(f"Removed {email}.")


# ---------------------------------------------------------------------------
# Google APIs
# ---------------------------------------------------------------------------


def api_get(account, url, service_key, opener=None, disable_on_error=False):
    """GET a Google API as JSON with the account's token; None when it fails.

    A 403 that will not fix itself (missing permission, API not enabled in the
    OAuth client's project) switches the service off for the account, so one bad
    combination does not cost a failing request on every keystroke.
    """
    opener = opener or urllib.request.urlopen
    for attempt in (0, 1):
        token = account.token(force=attempt == 1)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener(req, timeout=10) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            body = e.read()[:600].decode(errors="replace")
            lowered = body.lower()
            if e.code == 403 and "insufficient" in lowered:
                account.disabled_services.add(service_key)
                hint = ("GNOME Online Accounts no longer grants this access; use --setup instead"
                        if account.source == "goa" else "run --setup to authorize it again")
                log(f"[{account.identity}] token lacks the permission for {service_key}, "
                    f"skipping it for this account: {hint}", always=True)
            elif e.code == 403 and ("accessnotconfigured" in lowered or "service_disabled" in lowered
                                    or "has not been used in project" in lowered):
                account.disabled_services.add(service_key)
                log(f"[{account.identity}] the API behind {service_key} is not enabled in the Google "
                    "Cloud project of your OAuth client (APIs & Services > Library); skipping it",
                    always=True)
            elif disable_on_error:
                account.disabled_services.add(service_key)
                log(f"[{account.identity}] {service_key} is not available for this account "
                    f"(HTTP {e.code}), skipping it")
            else:
                log(f"[{account.identity}] {service_key}: HTTP {e.code}: {body[:200]!r}", always=True)
            return None
    return None


def account_url(url, email):
    """Make the browser open the link as the account that found it."""
    if not email:
        return url
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "authuser"]
    query.append(("authuser", email))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# -- Drive --------------------------------------------------------------------


def escape_term(term):
    return term.replace("\\", "\\\\").replace("'", "\\'")


def build_query(terms, mode):
    field = "fullText" if mode == "fulltext" else "name"
    clauses = [f"{field} contains '{escape_term(t)}'" for t in terms if t]
    clauses.append("trashed = false")
    return " and ".join(clauses)


def build_params(terms, cfg):
    params = {
        "q": build_query(terms, cfg["mode"]),
        "pageSize": cfg["max_results"],
        "fields": DRIVE_FIELDS,
    }
    # The API rejects orderBy together with fullText queries.
    if cfg["mode"] != "fulltext":
        params["orderBy"] = "modifiedTime desc"
    if cfg["shared_drives"]:
        params.update({
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "corpora": "allDrives",
        })
    return params


def drive_search(account, terms, cfg, opener=None):
    url = DRIVE_FILES_URL + "?" + urllib.parse.urlencode(build_params(terms, cfg))
    return (api_get(account, url, "drive", opener) or {}).get("files", [])


# ---------------------------------------------------------------------------
# Services: one search provider section each
# ---------------------------------------------------------------------------


class Service:
    """What the generic provider needs to know about one Google service."""

    key = ""            # config key and log label
    object_name = ""    # D-Bus object path suffix, also the conf file suffix
    label = ""
    permission = ""     # shown in --setup before the user enables it
    short = ""          # the same in a few words, for the checklist
    detail = ""         # a sentence or two shown under the checklist for the highlighted row
    api = ""            # name of the API to enable in Google Cloud
    request_scopes = ()
    accepted_scopes = ()

    @property
    def icon(self):
        return f"{APP_ID}.{self.object_name}"

    def scopes(self, cfg):
        """Scopes to request at login."""
        return list(self.request_scopes)

    def accepted(self, cfg):
        """Any one of these granted scopes is enough to search."""
        return set(self.accepted_scopes)

    def usable(self, account, cfg):
        if self.key in account.disabled_services:
            return False
        return account.scopes is None or bool(account.scopes & self.accepted(cfg))

    def search(self, account, terms, cfg):
        raise NotImplementedError

    def item_id(self, item):
        return item["id"]

    def sort(self, items):
        return items

    def meta(self, item, lang):
        """(name, description parts, icon name) of a result."""
        raise NotImplementedError

    def url(self, item):
        raise NotImplementedError

    def fallback_url(self, item_id):
        return self.home_url

    def search_url(self, terms):
        raise NotImplementedError


class DriveService(Service):
    key, object_name, label = "drive", "Drive", "Google Drive"
    short = "file names, owners and dates"
    detail = ("Sees the names, owners and dates of your files, never what is inside them. "
              "Searching inside files is a separate question, next.")
    permission = "file names, owners and dates (contents only if you ask for it)"
    api = "Google Drive API"
    home_url = "https://drive.google.com/"

    def scopes(self, cfg):
        return [SCOPE_READONLY if cfg["mode"] == "fulltext" else SCOPE_METADATA]

    def accepted(self, cfg):
        full = {SCOPE_READONLY, SCOPE_DRIVE}
        return full if cfg["mode"] == "fulltext" else full | {SCOPE_METADATA}

    def search(self, account, terms, cfg):
        return drive_search(account, terms, cfg)

    def sort(self, items):
        return sorted(items, key=lambda f: f.get("modifiedTime", ""), reverse=True)

    def meta(self, item, lang):
        mime = item.get("mimeType", "")
        owner = ", ".join(o.get("displayName", "") for o in item.get("owners", []) if o)
        when = (item.get("modifiedTime") or "")[:10]
        name = item.get("name", item["id"])
        return name, [kind_label(mime, lang), owner, when], ICONS.get(mime, "text-x-generic")

    def url(self, item):
        return item.get("webViewLink") or self.fallback_url(item["id"])

    def fallback_url(self, item_id):
        return f"https://drive.google.com/open?id={item_id}"

    def search_url(self, terms):
        return "https://drive.google.com/drive/search?q=" + urllib.parse.quote(" ".join(terms))


class GmailService(Service):
    key, object_name, label = "gmail", "Gmail", "Gmail"
    short = "needs to read all your mail"
    detail = ("Google has no permission to search mail without being able to read it, so "
              "this covers all your mail. Read-only; nothing is stored.")
    permission = "read access to ALL your mail (Google has no narrower permission that can search)"
    api = "Gmail API"
    home_url = "https://mail.google.com/"
    request_scopes = (SCOPE_GMAIL,)
    accepted_scopes = (SCOPE_GMAIL, "https://www.googleapis.com/auth/gmail.modify",
                       "https://mail.google.com/")
    HEADERS = ("Subject", "From", "Date")

    def search(self, account, terms, cfg):
        # Terms go to Gmail untouched, so its operators work: from:ana has:attachment
        params = {"q": " ".join(terms), "maxResults": cfg["max_results"]}
        listing = api_get(account, f"{GMAIL_URL}/messages?" + urllib.parse.urlencode(params), self.key)
        refs = (listing or {}).get("messages", [])
        if not refs:
            return []
        # The listing only carries ids; fetch the headers of all of them at once.
        query = urllib.parse.urlencode(
            [("format", "metadata")] + [("metadataHeaders", h) for h in self.HEADERS])

        def fetch(ref):
            return api_get(account, f"{GMAIL_URL}/messages/{ref['id']}?{query}", self.key)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(refs))) as pool:
            messages = [m for m in pool.map(fetch, refs) if m]
        items, seen = [], set()
        for message in messages:
            thread = message.get("threadId") or message["id"]
            if thread in seen:
                continue
            seen.add(thread)
            headers = {h["name"].lower(): h.get("value", "")
                       for h in message.get("payload", {}).get("headers", [])}
            items.append({
                "id": thread,
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "date": int(message.get("internalDate") or 0),
                "unread": "UNREAD" in message.get("labelIds", []),
            })
        return items

    def sort(self, items):
        return sorted(items, key=lambda m: m["date"], reverse=True)

    def meta(self, item, lang):
        name, address = email.utils.parseaddr(item.get("from", ""))
        when = ""
        if item.get("date"):
            when = datetime.fromtimestamp(item["date"] / 1000).strftime("%Y-%m-%d")
        subject = item.get("subject") or LABELS[lang]["no_subject"]
        return subject, [name or address, when], self.icon

    def url(self, item):
        return self.fallback_url(item["id"])

    def fallback_url(self, item_id):
        return f"https://mail.google.com/mail/#all/{item_id}"

    def search_url(self, terms):
        return "https://mail.google.com/mail/#search/" + urllib.parse.quote(" ".join(terms))


class CalendarService(Service):
    key, object_name, label = "calendar", "Calendar", "Google Calendar"
    short = "reads your events"
    detail = "Sees the events of your main calendar: titles, times and places. Read-only."
    permission = "read access to the events of your calendars"
    api = "Google Calendar API"
    home_url = "https://calendar.google.com/"
    request_scopes = (SCOPE_CALENDAR,)
    accepted_scopes = (SCOPE_CALENDAR, "https://www.googleapis.com/auth/calendar.readonly",
                       "https://www.googleapis.com/auth/calendar.events",
                       "https://www.googleapis.com/auth/calendar")
    PAST_DAYS = 90

    def _events(self, account, terms, **extra):
        params = {"q": " ".join(terms), "singleEvents": "true", "orderBy": "startTime", **extra}
        data = api_get(account, CALENDAR_EVENTS_URL + "?" + urllib.parse.urlencode(params), self.key)
        return (data or {}).get("items", [])

    def search(self, account, terms, cfg):
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        limit = cfg["max_results"]
        upcoming = self._events(account, terms, timeMin=stamp, maxResults=limit)
        for event in upcoming:
            event["_past"] = False
        if len(upcoming) >= limit or self.key in account.disabled_services:
            return upcoming
        # Fill what is left with the most recent past events.
        since = (now - timedelta(days=self.PAST_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        past = self._events(account, terms, timeMin=since, timeMax=stamp, maxResults=250)
        known = {e["id"] for e in upcoming}
        past = [e for e in past if e["id"] not in known][-(limit - len(upcoming)):]
        for event in past:
            event["_past"] = True
        return upcoming + past

    @staticmethod
    def _start(event):
        start = event.get("start") or {}
        return start.get("dateTime") or start.get("date") or ""

    def sort(self, items):
        upcoming = sorted((e for e in items if not e.get("_past")), key=self._start)
        past = sorted((e for e in items if e.get("_past")), key=self._start, reverse=True)
        return upcoming + past

    def meta(self, item, lang):
        start = self._start(item)
        when = start[:10] + (" " + start[11:16] if "T" in start else "")
        title = item.get("summary") or LABELS[lang]["no_title"]
        return title, [when.strip(), (item.get("location") or "")[:60]], self.icon

    def url(self, item):
        return item.get("htmlLink") or self.home_url

    def search_url(self, terms):
        return ("https://calendar.google.com/calendar/r/search?q="
                + urllib.parse.quote(" ".join(terms)))


class ContactsService(Service):
    key, object_name, label = "contacts", "Contacts", "Google Contacts"
    short = "reads contacts, people you wrote to, work directory"
    detail = ("Sees your saved contacts, the people you have exchanged mail with and, on work "
              "accounts, the company directory. Read-only.")
    permission = ("read access to your contacts, the people you have exchanged mail with and, "
                  "on work accounts, your organization's directory")
    api = "People API"
    home_url = "https://contacts.google.com/"
    request_scopes = (SCOPE_CONTACTS, SCOPE_OTHER_CONTACTS, SCOPE_DIRECTORY)
    accepted_scopes = (SCOPE_CONTACTS, "https://www.googleapis.com/auth/contacts")
    MASK = "names,emailAddresses,phoneNumbers,organizations"

    def _warm_up(self, account, endpoint, params, key):
        # The People API asks for an empty query first to build its search index.
        warmed = account.__dict__.setdefault("_people_warm", set())
        if endpoint not in warmed:
            warmed.add(endpoint)
            api_get(account, f"{PEOPLE_URL}/{endpoint}?"
                    + urllib.parse.urlencode({**params, "query": ""}), key, disable_on_error=True)

    def _optional(self, account, key, scope, endpoint, params, field, query, warm=False):
        """A source only some accounts have; whatever the error, stop asking."""
        granted = account.scopes is None or scope in account.scopes
        if not granted or key in account.disabled_services:
            return []
        if warm:
            self._warm_up(account, endpoint, params, key)
        if key in account.disabled_services:
            return []
        data = api_get(account, f"{PEOPLE_URL}/{endpoint}?"
                       + urllib.parse.urlencode({**params, "query": query}), key,
                       disable_on_error=True)
        found = (data or {}).get(field, [])
        return [r.get("person", r) for r in found]

    def search(self, account, terms, cfg):
        query = " ".join(terms)
        size = min(cfg["max_results"], 30)
        base = {"readMask": self.MASK, "pageSize": size}
        self._warm_up(account, "people:searchContacts", base, self.key)
        data = api_get(account, f"{PEOPLE_URL}/people:searchContacts?"
                       + urllib.parse.urlencode({**base, "query": query}), self.key)
        people = [r.get("person", {}) for r in (data or {}).get("results", [])]
        # People you have written to but never saved; most work contacts live here.
        people += self._optional(
            account, "other_contacts", SCOPE_OTHER_CONTACTS, "otherContacts:search",
            {"readMask": "names,emailAddresses,phoneNumbers", "pageSize": size}, "results", query,
            warm=True)
        # Personal accounts have no directory.
        people += self._optional(
            account, "directory", SCOPE_DIRECTORY, "people:searchDirectoryPeople",
            {**base, "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"}, "people", query)

        items, seen = [], set()
        for person in people:
            item = self._item(person)
            key = (item["email"] or item["id"]).lower()
            if item["id"] and key not in seen:
                seen.add(key)
                items.append(item)
        return items

    @staticmethod
    def _item(person):
        def first(field, key):
            values = person.get(field) or [{}]
            return values[0].get(key, "")

        org = ", ".join(p for p in (first("organizations", "title"), first("organizations", "name")) if p)
        return {
            "id": person.get("resourceName", ""),
            "name": first("names", "displayName"),
            "email": first("emailAddresses", "value"),
            "phone": first("phoneNumbers", "value"),
            "org": org,
        }

    def sort(self, items):
        return sorted(items, key=lambda c: (c["name"] or c["email"]).lower())

    def meta(self, item, lang):
        name = item["name"] or item["email"] or item["id"]
        email_part = item["email"] if item["name"] else ""
        return name, [email_part, item["phone"], item["org"]], self.icon

    def url(self, item):
        return self.fallback_url(item["id"])

    def fallback_url(self, item_id):
        return "https://contacts.google.com/person/" + urllib.parse.quote(item_id.split("/")[-1])

    def search_url(self, terms):
        return "https://contacts.google.com/search/" + urllib.parse.quote(" ".join(terms))


# In the order they are listed to the user: the ones enabled by default first.
SERVICES = [DriveService(), ContactsService(), GmailService(), CalendarService()]
SERVICES_BY_KEY = {service.key: service for service in SERVICES}


def enabled_services(cfg):
    return [s for s in SERVICES if cfg["services"].get(s.key)]


def login_scopes(cfg):
    """Everything the enabled services need, plus the address of the account."""
    scopes = [SCOPE_EMAIL]
    for service in enabled_services(cfg):
        scopes += [s for s in service.scopes(cfg) if s not in scopes]
    return scopes


# ---------------------------------------------------------------------------
# Opening results in the right browser profile
# ---------------------------------------------------------------------------


def chromium_profiles(config_dir):
    """Map account email -> profile directory from a Chromium "Local State" file."""
    try:
        with open(os.path.join(config_dir, "Local State")) as f:
            cache = json.load(f)["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    profiles = {}
    for directory, info in cache.items():
        email = (info or {}).get("user_name")
        if email:
            profiles.setdefault(email.lower(), directory)
    return profiles


def profile_command(app_id, commandline, url, email, overrides=None, home=None):
    """argv that opens url in the browser profile of email, or None to open it normally.

    Only Chromium-family browsers are handled: their profiles record the signed-in
    Google account and they accept --profile-directory on the command line.
    """
    if not email or not commandline or app_id not in CHROMIUM_BROWSERS:
        return None
    email = email.lower()
    directory = (overrides or {}).get(email)
    if not directory:
        config_dir = os.path.join(home or os.path.expanduser("~"), CHROMIUM_BROWSERS[app_id])
        directory = chromium_profiles(config_dir).get(email)
    if not directory:
        return None
    try:
        parts = shlex.split(commandline)
    except ValueError:
        return None
    # Drop desktop-entry field codes (%U, %u...) and Flatpak's @@ forwarding markers.
    argv = [a for a in parts if not (a.startswith("%") and len(a) == 2) and not a.startswith("@@")]
    if not argv:
        return None
    return argv + [f"--profile-directory={directory}", url]


def open_url(url, email=None, cfg=None):
    cfg = cfg or DEFAULTS
    if email and cfg.get("use_profiles", True):
        try:
            app = Gio.AppInfo.get_default_for_uri_scheme("https")
            argv = app and profile_command(
                app.get_id(), app.get_commandline(), url, email, cfg.get("profiles")
            )
            if argv:
                log(f"opening with profile: {argv[:-1]}")
                subprocess.Popen(  # noqa: S603
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                return
        except (OSError, GLib.Error) as e:
            log(f"could not open the browser profile, using the default handler: {e}", always=True)
    Gio.AppInfo.launch_default_for_uri(url, None)


# ---------------------------------------------------------------------------
# D-Bus search providers
# ---------------------------------------------------------------------------


class AccountManager:
    """Accounts from every source, shared by the providers of all services."""

    def __init__(self, cfg, quiet=False):
        self.cfg = cfg
        self.quiet = quiet  # the setup says it in its own words
        self.token_file = TokenFile(cfg["token_file"])
        self.last_activity = time.monotonic()
        self._accounts = None
        self._accounts_at = 0.0
        self._known = {}

    def touch(self):
        self.last_activity = time.monotonic()

    def invalidate(self):
        """Force the next all() call to re-read every source."""
        # Not a timestamp trick: time.monotonic() counts from boot and can be
        # smaller than the refresh interval on a machine that just started.
        self._accounts = None

    def all(self):
        """Every account. Re-read each minute so new logins show up without a restart."""
        if self._accounts is None or time.monotonic() - self._accounts_at > 60:
            found = stored_accounts()
            fallback = self.token_file.account()
            if fallback is not None:
                found.append(fallback)
            logged_in = {a.identity.lower() for a in found}
            found += [a for a in goa_accounts() if a.identity.lower() not in logged_in]
            # Keep the objects we already know: they hold cached tokens and the
            # services Google refused for them.
            known = {}
            for account in found:
                key = (account.source, account.identity)
                old = self._known.get(key)
                if old is not None:
                    old.scopes = account.scopes  # a new login may have granted more
                known[key] = old or account
            self._known = known
            self._accounts = list(known.values())
            self._accounts_at = time.monotonic()
            if not self._accounts and not self.quiet:
                log("no Google account: run 'gnome-google-workspace-search --setup'", always=True)
        return self._accounts


class SearchProvider:
    """org.gnome.Shell.SearchProvider2 for one service."""

    def __init__(self, loop, cfg, service=None, manager=None):
        self.loop = loop
        self.cfg = cfg
        self.service = service or SERVICES_BY_KEY["drive"]
        self.manager = manager or AccountManager(cfg)
        self.lang = ui_language()
        self.files = {}
        self.seq = 0
        self.pending = None

    # -- accounts -----------------------------------------------------------

    def invalidate_accounts(self):
        self.manager.invalidate()

    def accounts(self):
        """Accounts this service can search right now."""
        if not self.cfg["services"].get(self.service.key):
            return []
        return [a for a in self.manager.all() if self.service.usable(a, self.cfg)]

    # -- D-Bus dispatch -----------------------------------------------------

    def handle_call(self, conn, sender, path, iface, method, params, invocation):
        self.manager.touch()
        handler = getattr(self, method, None)
        if handler is None:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", f"unknown method {method}"
            )
            return
        try:
            handler(params, invocation)
        except Exception as e:  # noqa: BLE001
            log(f"{self.service.key}.{method} failed: {e!r}", always=True)
            invocation.return_dbus_error(f"{APP_ID}.Error", str(e))

    def _return_ids(self, invocation, ids):
        invocation.return_value(GLib.Variant("(as)", (ids,)))

    def _search_async(self, terms, invocation):
        query = [t.strip() for t in terms if t.strip()]
        if sum(len(t) for t in query) < self.cfg["min_chars"] or not self.accounts():
            self._return_ids(invocation, [])
            return
        self.seq += 1
        seq = self.seq
        # Only one search in flight; the superseded one answers with nothing.
        if self.pending is not None:
            self._return_ids(self.pending, [])
        self.pending = invocation

        def fire():
            if seq == self.seq:
                threading.Thread(target=work, daemon=True).start()
            return False

        def work():
            GLib.idle_add(finish, self.search_all(query))

        def finish(results):
            if seq != self.seq:
                return False
            ids = []
            for item in self.service.sort(results):
                item_id = self.service.item_id(item)
                if item_id in ids:
                    continue
                if len(ids) >= self.cfg["max_results"]:
                    break
                self.files[item_id] = item
                ids.append(item_id)
            self.pending = None
            self._return_ids(invocation, ids)
            return False

        GLib.timeout_add(self.cfg["debounce_ms"], fire)

    def search_all(self, terms):
        """Search every account at once; one slow or broken account does not block the rest."""
        accounts = self.accounts()
        if not accounts:
            return []

        def one(account):
            try:
                items = self.service.search(account, terms, self.cfg)
            except Exception as e:  # noqa: BLE001
                log(f"[{account.identity}] {self.service.key} search failed: {e!r}", always=True)
                return []
            address = account.resolve_email() if items else account.email
            for item in items:
                item["_account"] = address or account.identity
                item["_email"] = address
            return items

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(accounts)) as pool:
            return [item for items in pool.map(one, accounts) for item in items]

    # -- SearchProvider2 methods --------------------------------------------

    def GetInitialResultSet(self, params, invocation):
        (terms,) = params.unpack()
        self._search_async(terms, invocation)

    def GetSubsearchResultSet(self, params, invocation):
        (_previous, terms) = params.unpack()
        self._search_async(terms, invocation)

    def GetResultMetas(self, params, invocation):
        (ids,) = params.unpack()
        multi = len(self.accounts()) > 1
        metas = [self.result_meta(self.files[i], multi) for i in ids if i in self.files]
        invocation.return_value(GLib.Variant("(aa{sv})", (metas,)))

    def result_meta(self, item, multi_account=False):
        name, parts, icon = self.service.meta(item, self.lang)
        if multi_account:
            parts = list(parts) + [item.get("_account", "")]
        return {
            "id": GLib.Variant("s", self.service.item_id(item)),
            "name": GLib.Variant("s", name),
            "description": GLib.Variant("s", " - ".join(p for p in parts if p)),
            "gicon": GLib.Variant("s", icon),
        }

    def ActivateResult(self, params, invocation):
        (item_id, _terms, _ts) = params.unpack()
        item = self.files.get(item_id)
        url = self.service.url(item) if item else self.service.fallback_url(item_id)
        address = (item or {}).get("_email")
        self._open(account_url(url, address), address)
        invocation.return_value(None)

    def LaunchSearch(self, params, invocation):
        (terms, _ts) = params.unpack()
        self._open(self.service.search_url(terms))
        invocation.return_value(None)

    def _open(self, url, email=None):
        open_url(url, email, self.cfg)


# ---------------------------------------------------------------------------
# Guided setup
# ---------------------------------------------------------------------------

API_IDS = {"drive": "drive.googleapis.com", "gmail": "gmail.googleapis.com",
           "calendar": "calendar-json.googleapis.com", "contacts": "people.googleapis.com"}

WHY_A_CLIENT = """\
  Google only lets a program ask for access on behalf of an "OAuth client" that is
  registered in a Google Cloud project. This copy of the project does not ship one,
  so you register yours: free, about five minutes, only once. No file of yours is
  involved yet: the client only identifies the app; access is granted later, per
  account, in your browser."""

REFUSED_HINT = """\
  If Google refused the account, the usual reasons are:
    - "restricted to users within its organization" (org_internal): the OAuth client
      of project {project} is Internal, so it only accepts accounts of that Google
      Workspace organization. Use another client, of type External, for this account.
    - "has not completed the Google verification process" with no way forward: the
      app is in Testing and this account is not one of its test users. Publish the
      app, or add the account as a test user, in the Google Cloud console.
  Each account remembers the client it was connected with, so they can differ."""


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise KeyboardInterrupt from None
    return answer or default


def ask_yes_no(prompt, default=True):
    answer = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    if not answer:
        return default
    return answer in ("y", "yes", "s", "si", "sí")


# -- checklist ------------------------------------------------------------------

KEYS = {"\x1b[A": "up", "\x1b[B": "down", "\x1bOA": "up", "\x1bOB": "down", "k": "up", "j": "down",
        " ": "toggle", "x": "toggle", "X": "toggle", "a": "all", "A": "all",
        "\r": "done", "\n": "done"}


def checklist_available():
    """True on a real terminal that can be driven key by key."""
    try:
        import termios  # noqa: F401
        return (os.isatty(sys.stdin.fileno()) and os.isatty(sys.stdout.fileno())
                and os.environ.get("TERM", "dumb") != "dumb")
    except (ImportError, OSError, ValueError, AttributeError):
        return False


@contextlib.contextmanager
def key_mode():
    """Deliver keys one by one for the duration of the block, then restore the terminal.

    Entered once per checklist, not once per key: switching modes discards pending
    input, which would drop keys while an arrow is held down.
    """
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (ImportError, OSError, ValueError, AttributeError):
        yield  # not a terminal (tests, pipes): nothing to switch
        return
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # cbreak keeps Ctrl+C working, unlike raw mode
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


def read_key():
    """One key press, arrow keys included. Call inside key_mode()."""
    import select

    fd = sys.stdin.fileno()
    key = os.read(fd, 1).decode(errors="ignore")
    if key == "\x1b" and select.select([fd], [], [], 0.05)[0]:
        # An escape sequence arrives at once; a lone Esc has nothing behind it.
        key += os.read(fd, 2).decode(errors="ignore")
    return key


def checklist_lines(items, checked, cursor, width=80):
    lines = []
    pad = max(len(label) for _key, label, _note in items)
    for index, (key, label, note) in enumerate(items):
        mark = "x" if key in checked else " "
        pointer = ">" if index == cursor else " "
        lines.append(f"  {pointer} [{mark}] {label.ljust(pad)}  {note}"[:max(width - 1, 20)])
    return lines


DETAIL_LINES = 2


def detail_lines(text, width=80):
    """The explanation of the highlighted row, always DETAIL_LINES long so it redraws in place."""
    import textwrap

    room = max(width - 5, 20)
    wrapped = textwrap.wrap(text, room)
    if len(wrapped) > DETAIL_LINES:
        wrapped = wrapped[:DETAIL_LINES]
        wrapped[-1] = wrapped[-1][:room - 3].rstrip() + "..."
    wrapped += [""] * (DETAIL_LINES - len(wrapped))
    return ["    " + line for line in wrapped]


def checklist(items, checked, keys=None, out=None, width=None, details=None):
    """Pick any of items = [(key, label, note)]; returns the set of chosen keys.

    Arrows or j/k move, space or x toggles, a toggles all, Enter confirms.
    details maps a key to a sentence shown under the list while that row is highlighted.
    """
    mode = contextlib.nullcontext() if keys else key_mode()
    keys = keys or read_key
    out = out or sys.stdout
    width = width or shutil.get_terminal_size((80, 24)).columns
    checked, cursor = set(checked), 0
    out.write("  (arrows move, space or x marks, a marks all, Enter confirms)\n")
    with mode:
        return _checklist_loop(items, checked, cursor, keys, out, width, details)


def _checklist_loop(items, checked, cursor, keys, out, width, details=None):
    first = True
    while True:
        lines = checklist_lines(items, checked, cursor, width)
        extra = [""] + detail_lines(details.get(items[cursor][0], ""), width) if details else []
        if not first:
            out.write(f"\x1b[{len(lines) + len(extra)}A")  # back to the top of the list
        first = False
        for index, line in enumerate(lines):
            style = "\x1b[1m" if index == cursor else ""
            out.write(f"\r\x1b[2K{style}{line}\x1b[0m\n")
        for line in extra:
            out.write(f"\r\x1b[2K\x1b[2m{line}\x1b[0m\n")  # dimmed
        out.flush()
        action = KEYS.get(keys())
        if action == "up":
            cursor = (cursor - 1) % len(items)
        elif action == "down":
            cursor = (cursor + 1) % len(items)
        elif action == "toggle":
            checked ^= {items[cursor][0]}
        elif action == "all":
            everything = {key for key, _label, _note in items}
            checked = set() if checked == everything else everything
        elif action == "done":
            return checked


def choose_services(cfg):
    """Step 2 of the setup: which services to search."""
    if checklist_available():
        items = [(s.key, s.label, s.short) for s in SERVICES]
        current = {k for k, enabled in cfg["services"].items() if enabled}
        chosen = checklist(items, current, details={s.key: s.detail for s in SERVICES})
        for service in SERVICES:
            cfg["services"][service.key] = service.key in chosen
        print("  Selected: " + (", ".join(s.label for s in enabled_services(cfg)) or "nothing") + ".")
    else:
        # Plain question per service, for terminals that cannot be driven key by key.
        for service in SERVICES:
            print(f"  {service.label}: needs {service.permission}.")
            cfg["services"][service.key] = ask_yes_no(
                f"    Search {service.label}?", cfg["services"].get(service.key, False))
    if cfg["services"]["drive"]:
        fulltext = ask_yes_no("  Drive: also search inside file contents? Slower, and needs read "
                              "access to your files instead of names only", cfg["mode"] == "fulltext")
        cfg["mode"] = "fulltext" if fulltext else "name"
    else:
        cfg["mode"] = "name"


def provider_registration(service=None):
    """Path of the .ini GNOME Shell will load for a service, or None if it cannot see it."""
    service = service or SERVICES_BY_KEY["drive"]
    name = f"{APP_ID}.{service.object_name}.ini"
    for data_dir in GLib.get_system_data_dirs():
        path = os.path.join(data_dir, "gnome-shell", "search-providers", name)
        if os.path.exists(path):
            return path
    return None


def find_client_secret_candidates(home=None, since=0.0):
    """client_secret*.json files in ~/Downloads, newest first, downloaded after `since`.

    Older files are never offered: they belong to who knows which project.
    """
    downloads = os.path.join(home or os.path.expanduser("~"), "Downloads")
    try:
        names = [n for n in os.listdir(downloads)
                 if n.startswith("client_secret") and n.endswith(".json")]
    except OSError:
        return []
    paths = [os.path.join(downloads, n) for n in names]
    paths = [p for p in paths if os.path.getmtime(p) >= since]
    return sorted(paths, key=os.path.getmtime, reverse=True)


def creation_steps(cfg):
    """(title, url, instructions) of each page to visit to register an OAuth client."""
    apis = ",".join(API_IDS[s.key] for s in enabled_services(cfg)) or API_IDS["drive"]
    names = ", ".join(s.api for s in enabled_services(cfg)) or DriveService.api
    return [
        ("Project and APIs",
         "https://console.cloud.google.com/flows/enableapi?apiid=" + apis,
         [f"Pick 'Create project' (any name), continue, and press Enable. This turns on: {names}.",
          "A first-time Google Cloud user is asked to accept its terms; no billing is needed."]),
        ("Consent screen",
         "https://console.cloud.google.com/auth/overview",
         ["Press 'Get started'. App name: anything, it is what the login page will show.",
          "Audience: External. (Internal only accepts accounts of one Workspace organization.)",
          "Finish, then open 'Audience' in the left menu and press 'Publish app'.",
          "Skipping 'Publish app' leaves it in Testing: logins expire every 7 days."]),
        ("OAuth client",
         "https://console.cloud.google.com/auth/clients/create",
         ["Application type: 'Desktop app'. Create, then 'Download JSON'.",
          "Leave the file in your Downloads folder; the next step picks it up."]),
    ]


def guide_client_creation(cfg):
    """Walk through registering an OAuth client in the browser; returns its path or None."""
    started = time.time()
    print(WHY_A_CLIENT)
    owner = ask("\n  Google account that will own the client (a personal one accepts any account "
                "later; empty to skip opening pages)")
    for number, (title, url, lines) in enumerate(creation_steps(cfg), 1):
        print(f"\n  Step {number} of 3: {title}")
        target = account_url(url, owner or None)
        print(f"    {target}")
        for line in lines:
            print(f"    - {line}")
        if owner:
            try:
                open_url(target, owner, cfg)
            except GLib.Error as e:
                print(f"    (could not open the browser: {e.message}; open the link yourself)")
        ask("    Press Enter when that is done")
    while True:
        fresh = find_client_secret_candidates(since=started)
        path = ask("\n  Path to the JSON you just downloaded (empty to stop)", fresh[0] if fresh else "")
        if not path:
            return None
        path = os.path.expanduser(path)
        try:
            client = load_client_secret(path)
        except LoginError as e:
            print(f"  {e}")
            continue
        if client["kind"] == "web":
            print("  That is a 'Web application' client; this needs a 'Desktop app' one. Create it "
                  "again with the right type.")
            continue
        return path


def setup_client(store=None, cfg=None):
    """Make sure there is an OAuth client to log in with; False if the user gives up."""
    store = store or CLIENT_SECRET_PATH
    cfg = cfg or DEFAULTS
    apis = ", ".join(s.api for s in enabled_services(cfg)) or DriveService.api
    current = default_client_path(store)
    if os.path.exists(current):
        try:
            client = load_client_secret(current)
            origin = "yours" if current == store else "shipped with this install"
            print(f"  OAuth client: ready ({origin}, Google Cloud project {client['project']})")
            print(f"  That project must have these APIs enabled: {apis}.")
            return True
        except LoginError as e:
            print(f"  The stored OAuth client is unusable: {e}")
    path = guide_client_creation(cfg)
    if not path:
        return False
    os.makedirs(os.path.dirname(store), mode=0o700, exist_ok=True)
    shutil.copyfile(path, store)
    os.chmod(store, 0o600)
    print(f"  OAuth client saved to {store}")
    return True


def save_preferences(cfg, path=None):
    """Write the choices made in --setup, leaving every other key of the file alone."""
    path = path or CONFIG_PATH
    parser = configparser.ConfigParser()
    parser.read(path)
    for section in ("search", "services"):
        if not parser.has_section(section):
            parser.add_section(section)
    parser["search"]["mode"] = cfg["mode"]
    for key, enabled in cfg["services"].items():
        parser["services"][key] = "true" if enabled else "false"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w") as f:
        parser.write(f)


def account_scopes(email, accounts_dir=None):
    try:
        with open(account_path(email, accounts_dir)) as f:
            return json.load(f).get("scopes") or []
    except (OSError, ValueError):
        return []


def missing_services(account, cfg):
    """Enabled services this account has not granted (only knowable for token files)."""
    if account.scopes is None:
        return []
    return [s for s in enabled_services(cfg) if not account.scopes & s.accepted(cfg)]


def describe_account(account, cfg):
    text = f"{account.identity}"
    address = account.email
    if address and cfg.get("use_profiles", True):
        try:
            app = Gio.AppInfo.get_default_for_uri_scheme("https")
            argv = app and profile_command(app.get_id(), app.get_commandline(), "", address,
                                           cfg.get("profiles"))
        except GLib.Error:
            argv = None
        if argv:
            profile = argv[-2].split("=", 1)[1]
            text += f"  (opens in browser profile '{profile}')"
    if account.source != "login":
        text += f"  [{account.source}]"
    return text


def run_setup(cfg, config_path=None):
    """Interactive, re-runnable configuration: services, OAuth client, accounts, test."""
    if not sys.stdin.isatty():
        print("Error: --setup is interactive; run it in a terminal.", file=sys.stderr)
        return 1
    print("Google Workspace search for GNOME: setup\n")
    try:
        return _setup_steps(cfg, config_path or CONFIG_PATH)
    except KeyboardInterrupt:
        print("\nSetup interrupted. Run it again any time with --setup.")
        return 130


def _setup_steps(cfg, config_path):
    print("1. GNOME Shell integration")
    registered = {s.key: provider_registration(s) for s in SERVICES}
    if all(registered.values()):
        print("  OK: GNOME Shell can see the search sections, nothing to do here.")
        print(f"  (their definitions are installed in {os.path.dirname(registered['drive'])})")
    else:
        missing = ", ".join(s.label for s in SERVICES if not registered[s.key])
        print(f"  Not registered: GNOME Shell cannot see {missing} yet.\n"
              "  Run ./install.sh from the project directory and follow its last step.")

    print("\n2. Services (each one is its own section in the overview)")
    choose_services(cfg)
    if not enabled_services(cfg):
        print("  Nothing enabled, so there is nothing to search. Run --setup again to change it.")
        save_preferences(cfg, config_path)
        return 1
    save_preferences(cfg, config_path)

    print("\n3. OAuth client")
    have_client = setup_client(cfg=cfg)

    print("\n4. Google accounts")
    manager = AccountManager(cfg, quiet=True)
    scopes = login_scopes(cfg)

    def connect(hint=None):
        # An account is authorized again with the client it was connected with.
        client, path, tried = (client_of_account(hint) if hint else None), None, set()
        while True:
            using = client or load_client_secret(path or default_client_path())
            tried.add(using["client_id"])
            try:
                address = login(client_secret=path, client=client, scopes=scopes, login_hint=hint)
                break
            except KeyboardInterrupt:
                print("\n  Login cancelled.")
            except LoginError as e:
                print(f"  Login failed: {e}")
            print(REFUSED_HINT.format(project=using["project"]))
            others = [p for c, p in known_clients() if c["client_id"] not in tried]
            if others:
                path = os.path.expanduser(ask("  Path to another OAuth client for this account "
                                              "(empty to skip it)", others[0]))
            elif ask_yes_no("  Register another OAuth client now, for this account?", True):
                path = guide_client_creation(cfg)
                if path:
                    path = remember_client(path)
            else:
                path = ""
            if not path:
                return
            try:
                load_client_secret(path)
            except LoginError as e:
                print(f"  {e}")
                return
            client = None
        manager.invalidate()
        added = next((a for a in manager.all() if a.identity == address), None)
        if added:
            print(f"  + {describe_account(added, cfg)}")

    for account in manager.all():
        print(f"  - {describe_account(account, cfg)}")
        lacking = missing_services(account, cfg)
        if lacking and have_client and account.source == "login":
            names = ", ".join(s.label for s in lacking)
            if ask_yes_no(f"    It has not authorized {names}. Authorize now?", True):
                connect(account.identity)
        elif lacking:
            print(f"    Cannot search {', '.join(s.label for s in lacking)}: its token does not "
                  "cover them.")
    if not manager.all():
        print("  None yet.")
    add = have_client and ask_yes_no(
        "  Add an account?" if not manager.all() else "  Add another account?",
        default=not manager.all())
    while add:
        print("  Tip: with one browser profile per account, copy the link below into the "
              "right profile.")
        connect()
        add = ask_yes_no("  Add another account?", default=False)

    print("\n5. Test")
    manager.invalidate()
    providers = [SearchProvider(None, cfg, s, manager) for s in enabled_services(cfg)]
    if not any(p.accounts() for p in providers):
        print("  No usable account, nothing to test. Run --setup again when you have one.")
        return 1
    term = ask("  Type a word to try a search (empty to skip)")
    if term:
        print_results(providers, term.split(), limit=3, indent="    ")
    print("\nDone. Open the Activities overview and type to search."
          + ("" if all(registered.values()) else " (After registering the providers, see step 1.)"))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def print_results(providers, terms, limit=None, indent="", urls=False):
    total = 0
    for provider in providers:
        accounts = provider.accounts()
        items = provider.service.sort(provider.search_all(terms)) if accounts else []
        total += len(items)
        print(f"{indent}{provider.service.label}: {len(items)} result(s)"
              + ("" if accounts else " (no account can search it)"))
        for item in items[:limit]:
            meta = provider.result_meta(item, len(accounts) > 1)
            print(f"{indent}  {meta['name'].get_string()}  ({meta['description'].get_string()})")
            if urls:
                print(f"{indent}    {account_url(provider.service.url(item), item.get('_email'))}")
    return total


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="GNOME Shell search providers for Google Drive, Gmail, Calendar and Contacts")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    parser.add_argument("--config", default=CONFIG_PATH, help="path to config.ini")
    parser.add_argument("--setup", action="store_true",
                        help="guided setup: services, OAuth client, accounts and a test search")
    parser.add_argument(
        "--query", nargs="+", metavar="TERM",
        help="run one search from the command line and print the results (for debugging)",
    )
    parser.add_argument("--service", choices=sorted(SERVICES_BY_KEY),
                        help="with --query: search only this service (default: all enabled)")
    auth = parser.add_argument_group("accounts")
    auth.add_argument("--login", action="store_true",
                      help="add a Google account, or authorize an existing one again")
    auth.add_argument("--client-secret", metavar="FILE",
                      help="OAuth client JSON from Google Cloud; only needed on the first --login")
    auth.add_argument("--fulltext", action="store_true",
                      help="with --login: request read access to Drive contents (mode = fulltext)")
    auth.add_argument("--port", type=int, default=0,
                      help="with --login: fixed loopback port (only for 'Web application' clients)")
    auth.add_argument("--no-browser", action="store_true",
                      help="with --login: only print the link, do not open a browser")
    auth.add_argument("--accounts", action="store_true",
                      help="list the accounts and what each one can search")
    auth.add_argument("--logout", metavar="EMAIL", help="remove an account and revoke its token")
    return parser.parse_args(argv)


def run_query(cfg, terms, service_key=None):
    manager = AccountManager(cfg)
    if service_key:
        cfg["services"][service_key] = True
        services = [SERVICES_BY_KEY[service_key]]
    else:
        services = enabled_services(cfg)
    providers = [SearchProvider(None, cfg, s, manager) for s in services]
    if not manager.all():
        return 1
    print_results(providers, terms, urls=True)
    return 0


def run_accounts(cfg):
    manager = AccountManager(cfg)
    labels = {"login": "--login", "token_file": "auth.token_file", "goa": "GNOME Online Accounts"}
    if not manager.all():
        print("No accounts. Add one with: gnome-google-workspace-search --setup")
        return 1
    for account in manager.all():
        can = [s.label for s in enabled_services(cfg) if s.usable(account, cfg)]
        print(f"{account.identity}  ({labels.get(account.source, account.source)})  "
              f"searches: {', '.join(can) or 'nothing enabled'}")
        lacking = missing_services(account, cfg)
        if lacking:
            print(f"    not authorized for: {', '.join(s.label for s in lacking)} (run --setup)")
    return 0


def migrate_legacy_config():
    """The project used to be gnome-drive-search-provider; bring its config along."""
    if os.path.isdir(LEGACY_CONFIG_DIR) and not os.path.exists(CONFIG_DIR):
        shutil.move(LEGACY_CONFIG_DIR, CONFIG_DIR)
        log(f"moved {LEGACY_CONFIG_DIR} to {CONFIG_DIR}", always=True)


def main(argv=None):
    global VERBOSE
    args = parse_args(sys.argv[1:] if argv is None else argv)
    VERBOSE = args.verbose
    migrate_legacy_config()
    cfg = load_config(args.config)
    if args.setup:
        return run_setup(cfg, args.config)
    try:
        if args.login:
            if args.fulltext:
                cfg["mode"] = "fulltext"
            login(args.client_secret, port=args.port, open_browser=not args.no_browser,
                  scopes=login_scopes(cfg))
            return 0
        if args.logout:
            logout(args.logout)
            return 0
    except LoginError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.accounts:
        return run_accounts(cfg)
    if args.query:
        return run_query(cfg, args.query, args.service)

    loop = GLib.MainLoop()
    manager = AccountManager(cfg)
    providers = [SearchProvider(loop, cfg, service, manager) for service in SERVICES]
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)

    def on_bus_acquired(conn, name):
        for provider in providers:
            path = f"{OBJECT_PATH}/{provider.service.object_name}"
            conn.register_object(path, node.interfaces[0], provider.handle_call, None, None)

    def on_name_lost(conn, name):
        log("bus name lost, exiting", always=True)
        loop.quit()

    def maybe_exit():
        if time.monotonic() - manager.last_activity > cfg["idle_exit_seconds"]:
            log("idle, exiting")
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(30, maybe_exit)
    Gio.bus_own_name(
        Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired, None, on_name_lost,
    )
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
