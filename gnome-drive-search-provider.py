#!/usr/bin/env python3
"""GNOME Shell search provider for Google Drive.

Implements org.gnome.Shell.SearchProvider2 over D-Bus so that typing in the
Activities overview searches the files in your Google Drive.

Accounts are added with ``--login``, which runs the OAuth flow in your browser
using your own OAuth client and stores one token file per account under
``~/.config/gnome-drive-search-provider/accounts``. Every logged-in account is
searched. Two more sources are supported for compatibility:

- ``auth.token_file``: an existing "authorized_user" JSON token (google-auth).
- GNOME Online Accounts, on the old GNOME releases whose Google tokens still
  carry a Drive scope. Current releases do not, and such accounts are skipped.

The process is started on demand by D-Bus activation and exits after a period
of inactivity.
"""

import argparse
import base64
import concurrent.futures
import configparser
import hashlib
import http.server
import json
import locale
import os
import secrets
import shutil
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

APP_ID = "io.github.jarrieta86.DriveSearchProvider"
BUS_NAME = APP_ID
OBJECT_PATH = "/" + APP_ID.replace(".", "/")

GOA_BUS_NAME = "org.gnome.OnlineAccounts"
GOA_OBJECT_PATH = "/org/gnome/OnlineAccounts"
GOA_ACCOUNT_IFACE = "org.gnome.OnlineAccounts.Account"
GOA_OAUTH2_IFACE = "org.gnome.OnlineAccounts.OAuth2Based"

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_FIELDS = "files(id,name,mimeType,webViewLink,modifiedTime,owners(displayName))"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Least privilege: file names and metadata only. Full-text search needs read access.
SCOPE_METADATA = "https://www.googleapis.com/auth/drive.metadata.readonly"
SCOPE_READONLY = "https://www.googleapis.com/auth/drive.readonly"

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "gnome-drive-search-provider")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")
ACCOUNTS_DIR = os.path.join(CONFIG_DIR, "accounts")
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, "client_secret.json")

DEFAULTS = {
    "mode": "name",  # "name" matches file names, "fulltext" also matches content
    "max_results": 10,
    "min_chars": 3,
    "debounce_ms": 250,
    "shared_drives": True,
    "idle_exit_seconds": 300,
    "token_file": "",
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
    },
    "es": {
        "document": "Documento", "spreadsheet": "Hoja de cálculo", "presentation": "Presentación",
        "form": "Formulario", "folder": "Carpeta", "drawing": "Dibujo", "shortcut": "Acceso directo",
        "pdf": "PDF", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint",
        "text": "Texto", "csv": "CSV", "image": "Imagen", "video": "Video", "audio": "Audio",
        "file": "Archivo",
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
        print(f"[gnome-drive-search-provider] {msg}", file=sys.stderr, flush=True)


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

    def __init__(self, identity, token_getter, source="login"):
        self.identity = identity
        self.source = source
        # Set when Google says the token cannot access Drive, so we stop asking.
        self.disabled = False
        self._token_getter = token_getter
        self._token = None
        self._expires_at = 0.0

    @property
    def email(self):
        return self.identity if "@" in self.identity else None

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
        return Account(identity, self.fresh_token, source=self.source)

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
    }


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
    req = urllib.request.Request(
        DRIVE_ABOUT_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)["user"]["emailAddress"]
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise LoginError(
            f"logged in, but Drive refused the token (HTTP {e.code}). Is the Google Drive "
            f"API enabled in your Google Cloud project? {detail}"
        ) from None


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
          accounts_dir=None, client_secret_store=None):
    """Add (or re-authorize) a Google account. Returns its email."""
    store = client_secret_store or CLIENT_SECRET_PATH
    client = load_client_secret(client_secret or store)
    scope = SCOPE_READONLY if fulltext else SCOPE_METADATA
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)

    server = http.server.HTTPServer(("127.0.0.1", port), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = build_auth_url(client, redirect_uri, scope, state, challenge)
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
        print("Waiting for you to approve access...", flush=True)
        code = wait_for_redirect(server, state, timeout)
    finally:
        server.server_close()

    payload = exchange_code(client, code, verifier, redirect_uri)
    email = fetch_email(payload["access_token"])
    path = save_account(email, client, payload, scope, accounts_dir)
    if client_secret and os.path.abspath(client_secret) != os.path.abspath(store):
        os.makedirs(os.path.dirname(store), exist_ok=True)
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
# Drive API
# ---------------------------------------------------------------------------


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


def drive_search(account, terms, cfg, opener=urllib.request.urlopen):
    url = DRIVE_FILES_URL + "?" + urllib.parse.urlencode(build_params(terms, cfg))
    for attempt in (0, 1):
        token = account.token(force=attempt == 1)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener(req, timeout=10) as resp:
                return json.load(resp).get("files", [])
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            body = e.read()[:400].decode(errors="replace")
            if e.code == 403 and "insufficient" in body.lower():
                account.disabled = True
                hint = ("GNOME Online Accounts no longer grants Drive access; use --login instead"
                        if account.source == "goa" else
                        "log in again, adding --fulltext if you use mode = fulltext")
                log(f"[{account.identity}] token lacks the Drive permission for this search, "
                    f"skipping this account: {hint}", always=True)
            else:
                log(f"[{account.identity}] HTTP {e.code}: {body[:200]!r}", always=True)
            return []
    return []


def account_url(url, email):
    """Make the browser open the file with the account that can see it."""
    if not email:
        return url
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "authuser"]
    query.append(("authuser", email))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# ---------------------------------------------------------------------------
# D-Bus search provider
# ---------------------------------------------------------------------------


class SearchProvider:
    def __init__(self, loop, cfg):
        self.loop = loop
        self.cfg = cfg
        self.lang = ui_language()
        self.token_file = TokenFile(cfg["token_file"])
        self.files = {}
        self.seq = 0
        self.pending = None
        self.last_activity = time.monotonic()
        self._accounts = None
        self._accounts_at = 0.0
        self._known = {}
        GLib.timeout_add_seconds(30, self._maybe_exit)

    # -- accounts -----------------------------------------------------------

    def accounts(self):
        """Usable accounts. Re-read every minute so --login shows up without a restart."""
        if self._accounts is None or time.monotonic() - self._accounts_at > 60:
            found = stored_accounts()
            fallback = self.token_file.account()
            if fallback is not None:
                found.append(fallback)
            logged_in = {a.identity.lower() for a in found}
            found += [a for a in goa_accounts() if a.identity.lower() not in logged_in]
            # Keep the objects we already know: they hold cached tokens and the
            # "disabled" flag of accounts that cannot access Drive.
            known = {}
            for account in found:
                key = (account.source, account.identity)
                known[key] = self._known.get(key, account)
            self._known = known
            self._accounts = list(known.values())
            self._accounts_at = time.monotonic()
            if not self._accounts:
                log("no Google account: run 'gnome-drive-search-provider --login'", always=True)
        return [a for a in self._accounts if not a.disabled]

    # -- lifecycle ----------------------------------------------------------

    def _touch(self):
        self.last_activity = time.monotonic()

    def _maybe_exit(self):
        if time.monotonic() - self.last_activity > self.cfg["idle_exit_seconds"]:
            log("idle, exiting")
            self.loop.quit()
            return False
        return True

    # -- D-Bus dispatch -----------------------------------------------------

    def handle_call(self, conn, sender, path, iface, method, params, invocation):
        self._touch()
        handler = getattr(self, method, None)
        if handler is None:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", f"unknown method {method}"
            )
            return
        try:
            handler(params, invocation)
        except Exception as e:  # noqa: BLE001
            log(f"{method} failed: {e!r}", always=True)
            invocation.return_dbus_error(f"{APP_ID}.Error", str(e))

    def _return_ids(self, invocation, ids):
        invocation.return_value(GLib.Variant("(as)", (ids,)))

    def _search_async(self, terms, invocation):
        query = [t.strip() for t in terms if t.strip()]
        if sum(len(t) for t in query) < self.cfg["min_chars"]:
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
            results.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
            ids = []
            for f in results[: self.cfg["max_results"]]:
                if f["id"] in ids:
                    continue
                self.files[f["id"]] = f
                ids.append(f["id"])
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
                files = drive_search(account, terms, self.cfg)
            except Exception as e:  # noqa: BLE001
                log(f"[{account.identity}] search failed: {e!r}", always=True)
                return []
            for f in files:
                f["_account"] = account.identity
                f["_email"] = account.email
            return files

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(accounts)) as pool:
            return [f for files in pool.map(one, accounts) for f in files]

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

    def result_meta(self, f, multi_account=False):
        mime = f.get("mimeType", "")
        owner = ", ".join(o.get("displayName", "") for o in f.get("owners", []) if o)
        when = (f.get("modifiedTime") or "")[:10]
        parts = [kind_label(mime, self.lang), owner, when]
        if multi_account:
            parts.append(f.get("_account", ""))
        return {
            "id": GLib.Variant("s", f["id"]),
            "name": GLib.Variant("s", f.get("name", f["id"])),
            "description": GLib.Variant("s", " - ".join(p for p in parts if p)),
            "gicon": GLib.Variant("s", ICONS.get(mime, "text-x-generic")),
        }

    def ActivateResult(self, params, invocation):
        (fid, _terms, _ts) = params.unpack()
        f = self.files.get(fid, {})
        url = f.get("webViewLink") or f"https://drive.google.com/open?id={fid}"
        self._open(account_url(url, f.get("_email")))
        invocation.return_value(None)

    def LaunchSearch(self, params, invocation):
        (terms, _ts) = params.unpack()
        q = urllib.parse.quote(" ".join(terms))
        self._open(f"https://drive.google.com/drive/search?q={q}")
        invocation.return_value(None)

    @staticmethod
    def _open(url):
        Gio.AppInfo.launch_default_for_uri(url, None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(description="GNOME Shell search provider for Google Drive")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    parser.add_argument("--config", default=CONFIG_PATH, help="path to config.ini")
    parser.add_argument(
        "--query", nargs="+", metavar="TERM",
        help="run one search from the command line and print the results (for debugging)",
    )
    auth = parser.add_argument_group("accounts")
    auth.add_argument("--login", action="store_true",
                      help="add a Google account (run it once per account)")
    auth.add_argument("--client-secret", metavar="FILE",
                      help="OAuth client JSON from Google Cloud; only needed on the first --login")
    auth.add_argument("--fulltext", action="store_true",
                      help="with --login: also request read access, needed for mode = fulltext")
    auth.add_argument("--port", type=int, default=0,
                      help="with --login: fixed loopback port (only for 'Web application' clients)")
    auth.add_argument("--no-browser", action="store_true",
                      help="with --login: only print the link, do not open a browser")
    auth.add_argument("--accounts", action="store_true", help="list the accounts being searched")
    auth.add_argument("--logout", metavar="EMAIL", help="remove an account and revoke its token")
    return parser.parse_args(argv)


def run_query(cfg, terms):
    provider = SearchProvider(GLib.MainLoop(), cfg)
    accounts = provider.accounts()
    if not accounts:
        return 1
    files = provider.search_all(terms)
    files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    for f in files:
        meta = provider.result_meta(f, len(accounts) > 1)
        url = account_url(f.get("webViewLink", ""), f.get("_email"))
        print(f"{meta['name'].get_string()}\n    {meta['description'].get_string()}\n    {url}")
    return 0


def run_accounts(cfg):
    provider = SearchProvider(GLib.MainLoop(), cfg)
    provider.accounts()
    labels = {"login": "--login", "token_file": "auth.token_file", "goa": "GNOME Online Accounts"}
    if not provider._accounts:
        print("No accounts. Add one with: gnome-drive-search-provider --login")
        return 1
    for account in provider._accounts:
        print(f"{account.identity}  ({labels.get(account.source, account.source)})")
    return 0


def main(argv=None):
    global VERBOSE
    args = parse_args(sys.argv[1:] if argv is None else argv)
    VERBOSE = args.verbose
    cfg = load_config(args.config)
    try:
        if args.login:
            login(args.client_secret, fulltext=args.fulltext, port=args.port,
                  open_browser=not args.no_browser)
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
        return run_query(cfg, args.query)

    loop = GLib.MainLoop()
    provider = SearchProvider(loop, cfg)
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)

    def on_bus_acquired(conn, name):
        conn.register_object(OBJECT_PATH, node.interfaces[0], provider.handle_call, None, None)

    def on_name_lost(conn, name):
        log("bus name lost, exiting", always=True)
        loop.quit()

    Gio.bus_own_name(
        Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired, None, on_name_lost,
    )
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
