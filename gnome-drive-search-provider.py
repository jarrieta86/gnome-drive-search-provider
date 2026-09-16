#!/usr/bin/env python3
"""GNOME Shell search provider for Google Drive.

Implements org.gnome.Shell.SearchProvider2 over D-Bus so that typing in the
Activities overview searches the files in your Google Drive.

Authentication, in order of preference:

1. GNOME Online Accounts: every Google account added in Settings > Online
   Accounts with "Files" enabled is searched. No configuration needed.
2. A token file in the "authorized_user" JSON format produced by google-auth,
   gcloud and similar tools (fields: client_id, client_secret, refresh_token).
   Configure it with ``token_file`` in the config file or with the
   ``GNOME_DRIVE_SEARCH_TOKEN_FILE`` environment variable.

The process is started on demand by D-Bus activation and exits after a period
of inactivity.
"""

import argparse
import configparser
import json
import locale
import os
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
TOKEN_URL = "https://oauth2.googleapis.com/token"

CONFIG_PATH = os.path.join(
    GLib.get_user_config_dir(), "gnome-drive-search-provider", "config.ini"
)

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

    def __init__(self, identity, token_getter):
        self.identity = identity
        self._token_getter = token_getter
        self._token = None
        self._expires_at = 0.0

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
        accounts.append(Account(identity, _goa_token_getter(bus, path)))
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

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def account(self):
        if not self.path or not os.path.exists(self.path):
            return None
        return Account(os.path.basename(self.path), self.fresh_token)

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
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
        log("token file refreshed")
        return self.data["token"], expires_in


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
            log(f"[{account.identity}] HTTP {e.code}: {e.read()[:200]!r}", always=True)
            return []
    return []


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
        GLib.timeout_add_seconds(30, self._maybe_exit)

    # -- accounts -----------------------------------------------------------

    def accounts(self):
        # Re-read GOA every minute so newly added accounts show up without a restart.
        if self._accounts is None or time.monotonic() - self._accounts_at > 60:
            accounts = goa_accounts()
            fallback = self.token_file.account()
            if fallback is not None:
                accounts.append(fallback)
            if not accounts:
                log("no Google account found: add one in Settings > Online Accounts "
                    "or set auth.token_file", always=True)
            self._accounts = accounts
            self._accounts_at = time.monotonic()
        return self._accounts

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
            results = []
            for account in self.accounts():
                try:
                    for f in drive_search(account, query, self.cfg):
                        f["_account"] = account.identity
                        results.append(f)
                except Exception as e:  # noqa: BLE001
                    log(f"[{account.identity}] search failed: {e!r}", always=True)
            GLib.idle_add(finish, results)

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
        self._open(url)
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
    return parser.parse_args(argv)


def run_query(cfg, terms):
    provider = SearchProvider(GLib.MainLoop(), cfg)
    accounts = provider.accounts()
    if not accounts:
        return 1
    for account in accounts:
        for f in drive_search(account, terms, cfg):
            meta = provider.result_meta(f, len(accounts) > 1)
            print(f"{meta['name'].get_string()}\n    {meta['description'].get_string()}\n"
                  f"    {f.get('webViewLink', '')}")
    return 0


def main(argv=None):
    global VERBOSE
    args = parse_args(sys.argv[1:] if argv is None else argv)
    VERBOSE = args.verbose
    cfg = load_config(args.config)
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
