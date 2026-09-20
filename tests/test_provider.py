import importlib.util
import io
import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "gnome-google-workspace-search.py"
SERVICE_NAMES = ["Drive", "Contacts", "Gmail", "Calendar"]

spec = importlib.util.spec_from_file_location("provider", SCRIPT)
provider = importlib.util.module_from_spec(spec)
sys.modules["provider"] = provider
spec.loader.exec_module(provider)

GLib = provider.GLib


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def test_build_query_escapes_quotes_and_backslashes():
    q = provider.build_query(["o'neil", "a\\b"], "name")
    assert q == "name contains 'o\\'neil' and name contains 'a\\\\b' and trashed = false"


def test_build_query_fulltext_mode():
    assert provider.build_query(["x"], "fulltext").startswith("fullText contains 'x'")


def test_build_params_name_mode_sorts_and_includes_shared_drives():
    cfg = dict(provider.DEFAULTS)
    params = provider.build_params(["a"], cfg)
    assert params["orderBy"] == "modifiedTime desc"
    assert params["corpora"] == "allDrives"
    assert params["pageSize"] == 10


def test_build_params_fulltext_has_no_order_by_and_can_skip_shared_drives():
    cfg = dict(provider.DEFAULTS, mode="fulltext", shared_drives=False)
    params = provider.build_params(["a"], cfg)
    assert "orderBy" not in params
    assert "corpora" not in params


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_load_config_defaults_when_file_missing(tmp_path):
    cfg = provider.load_config(str(tmp_path / "missing.ini"))
    assert cfg["mode"] == "name"
    assert cfg["max_results"] == 10


def test_load_config_reads_values_and_expands_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GNOME_DRIVE_SEARCH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    ini = tmp_path / "config.ini"
    ini.write_text(
        "[search]\nmode = fulltext\nmax_results = 5\nshared_drives = no\n"
        "[auth]\ntoken_file = ~/token.json\n"
    )
    cfg = provider.load_config(str(ini))
    assert cfg["mode"] == "fulltext"
    assert cfg["max_results"] == 5
    assert cfg["shared_drives"] is False
    assert cfg["token_file"] == str(tmp_path / "token.json")


def test_load_config_env_overrides_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GNOME_DRIVE_SEARCH_TOKEN_FILE", "/tmp/x.json")
    cfg = provider.load_config(str(tmp_path / "missing.ini"))
    assert cfg["token_file"] == "/tmp/x.json"


def test_load_config_rejects_unknown_mode(tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text("[search]\nmode = regex\n")
    assert provider.load_config(str(ini))["mode"] == "name"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_ui_language_prefers_LANGUAGE_and_falls_back_to_en(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "es_CL:es")
    assert provider.ui_language() == "es"
    monkeypatch.setenv("LANGUAGE", "de_DE.UTF-8")
    assert provider.ui_language() == "en"


def test_kind_label_known_and_generic_mimes():
    assert provider.kind_label("application/vnd.google-apps.spreadsheet", "es") == "Hoja de cálculo"
    assert provider.kind_label("image/png", "en") == "Image"
    assert provider.kind_label("application/x-whatever", "en") == "File"


# ---------------------------------------------------------------------------
# Token file backend
# ---------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_token_file_refreshes_when_expired(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "client_id": "id", "client_secret": "secret", "refresh_token": "rt",
        "token": "old", "expiry": "2000-01-01T00:00:00Z",
    }))
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        return FakeResponse(json.dumps({"access_token": "new", "expires_in": 3600}).encode())

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    tf = provider.TokenFile(str(path))
    token, expires_in = tf.fresh_token()
    assert token == "new" and expires_in == 3600
    assert len(calls) == 1
    assert b"grant_type=refresh_token" in calls[0].data
    saved = json.loads(path.read_text())
    assert saved["token"] == "new" and saved["expiry"].endswith("Z")
    # A second call within the validity window does not hit the network.
    tf.fresh_token()
    assert len(calls) == 1


def test_token_file_missing_keys(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"refresh_token": "rt"}))
    with pytest.raises(ValueError):
        provider.TokenFile(str(path)).fresh_token()


def test_token_file_account_is_none_without_path():
    assert provider.TokenFile("").account() is None


# ---------------------------------------------------------------------------
# Drive search
# ---------------------------------------------------------------------------


def make_account(tokens):
    it = iter(tokens)
    return provider.Account("me@example.com", lambda: (next(it), 3600))


def test_drive_search_retries_once_on_401(monkeypatch):
    account = make_account(["stale", "fresh"])
    seen = []

    def opener(req, timeout=0):
        seen.append(req.get_header("Authorization"))
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b""))
        return FakeResponse(json.dumps({"files": [{"id": "1", "name": "A"}]}).encode())

    files = provider.drive_search(account, ["a"], dict(provider.DEFAULTS), opener=opener)
    assert [f["id"] for f in files] == ["1"]
    assert seen == ["Bearer stale", "Bearer fresh"]


def test_drive_search_returns_empty_on_other_http_errors():
    account = make_account(["t"])

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"nope"))

    assert provider.drive_search(account, ["a"], dict(provider.DEFAULTS), opener=opener) == []


def test_account_caches_token_until_expiry():
    calls = []

    def getter():
        calls.append(1)
        return "tok", 3600

    account = provider.Account("x", getter)
    assert account.token() == "tok"
    assert account.token() == "tok"
    assert len(calls) == 1
    account.token(force=True)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# SearchProvider (D-Bus side)
# ---------------------------------------------------------------------------


class FakeInvocation:
    def __init__(self):
        self.value = None
        self.error = None

    def return_value(self, variant):
        self.value = variant.unpack() if variant is not None else None

    def return_dbus_error(self, name, message):
        self.error = (name, message)


def make_provider(monkeypatch, files, **cfg_overrides):
    cfg = dict(provider.DEFAULTS, debounce_ms=1, **cfg_overrides)
    sp = provider.SearchProvider(GLib.MainLoop(), cfg)
    sp.manager._accounts = [make_account(["t"] * 10)]
    sp.manager._accounts_at = float("inf")
    monkeypatch.setattr(provider, "drive_search", lambda account, terms, cfg, **kw: list(files))
    return sp


def pump(seconds=0.3):
    ctx = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(seconds * 1e6)
    while GLib.get_monotonic_time() < deadline:
        ctx.iteration(False)


def test_search_returns_ids_sorted_by_date_and_metas(monkeypatch):
    files = [
        {"id": "old", "name": "Old", "mimeType": "application/pdf", "modifiedTime": "2020-01-01T00:00:00Z",
         "owners": [{"displayName": "Ann"}], "webViewLink": "https://x/old"},
        {"id": "new", "name": "New", "mimeType": "application/vnd.google-apps.folder",
         "modifiedTime": "2024-01-01T00:00:00Z"},
    ]
    sp = make_provider(monkeypatch, files)
    sp.lang = "en"
    inv = FakeInvocation()
    sp.handle_call(None, None, None, None, "GetInitialResultSet",
                   GLib.Variant("(as)", (["report"],)), inv)
    pump()
    assert inv.value == (["new", "old"],)

    metas = FakeInvocation()
    sp.handle_call(None, None, None, None, "GetResultMetas",
                   GLib.Variant("(as)", (["old", "missing"],)), metas)
    (result,) = metas.value
    assert result == [{
        "id": "old", "name": "Old", "description": "PDF - Ann - 2020-01-01", "gicon": "application-pdf",
    }]


def test_short_query_returns_nothing_without_searching(monkeypatch):
    sp = make_provider(monkeypatch, [{"id": "1", "name": "x"}])
    inv = FakeInvocation()
    sp.GetInitialResultSet(GLib.Variant("(as)", (["ab"],)), inv)
    assert inv.value == ([],)


def test_newer_search_supersedes_pending_one(monkeypatch):
    sp = make_provider(monkeypatch, [{"id": "1", "name": "x", "modifiedTime": ""}])
    first, second = FakeInvocation(), FakeInvocation()
    sp.GetInitialResultSet(GLib.Variant("(as)", (["abc"],)), first)
    sp.GetSubsearchResultSet(GLib.Variant("(asas)", (["1"], ["abcd"])), second)
    assert first.value == ([],)
    pump()
    assert second.value == (["1"],)


def test_unknown_method_returns_dbus_error(monkeypatch):
    sp = make_provider(monkeypatch, [])
    inv = FakeInvocation()
    sp.handle_call(None, None, None, None, "Nope", GLib.Variant("()", ()), inv)
    assert inv.error[0] == "org.freedesktop.DBus.Error.UnknownMethod"


def test_activate_result_opens_web_link(monkeypatch):
    sp = make_provider(monkeypatch, [])
    opened = []
    monkeypatch.setattr(sp, "_open", lambda url, email=None: opened.append(url))
    sp.files["1"] = {"id": "1", "webViewLink": "https://docs.google.com/d/1"}
    inv = FakeInvocation()
    sp.ActivateResult(GLib.Variant("(sasu)", ("1", ["x"], 0)), inv)
    sp.ActivateResult(GLib.Variant("(sasu)", ("2", ["x"], 0)), inv)
    sp.LaunchSearch(GLib.Variant("(asu)", (["a b"], 0)), inv)
    assert opened == [
        "https://docs.google.com/d/1",
        "https://drive.google.com/open?id=2",
        "https://drive.google.com/drive/search?q=a%20b",
    ]


def test_goa_accounts_returns_empty_when_service_missing(monkeypatch):
    class Bus:
        pass

    def fail(*a, **kw):
        raise GLib.Error("no such service")

    monkeypatch.setattr(provider.Gio.DBusProxy, "new_sync", fail)
    assert provider.goa_accounts(bus=Bus()) == []


def test_conf_files_agree_on_ids():
    conf = ROOT / "conf"
    for name in SERVICE_NAMES:
        ini = (conf / f"{provider.APP_ID}.{name}.ini").read_text()
        assert f"BusName={provider.BUS_NAME}" in ini
        assert f"ObjectPath={provider.OBJECT_PATH}/{name}\n" in ini
        assert f"DesktopId={provider.APP_ID}.{name}.desktop" in ini
        assert (conf / f"{provider.APP_ID}.{name}.desktop").exists()
        assert (ROOT / "icons" / f"{provider.APP_ID}.{name}.svg").exists()
    assert [s.object_name for s in provider.SERVICES] == SERVICE_NAMES
    assert f"Name={provider.BUS_NAME}" in (conf / f"{provider.APP_ID}.service.in").read_text()
    assert os.access(SCRIPT, os.X_OK)


def test_desktop_files_are_accepted_by_gnome_shell(monkeypatch):
    # GNOME Shell drops providers whose desktop file fails should_show(), which
    # is the case with NoDisplay=true or when OnlyShowIn excludes GNOME.
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("GioUnix", "2.0")
        from gi.repository import GioUnix
        loader = GioUnix.DesktopAppInfo
    except (ValueError, ImportError):
        loader = provider.Gio.DesktopAppInfo
    for name in SERVICE_NAMES:
        path = ROOT / "conf" / f"{provider.APP_ID}.{name}.desktop"
        assert "NoDisplay" not in path.read_text()
        info = loader.new_from_filename(str(path))
        assert info is not None and info.should_show()
        assert f"Icon={provider.APP_ID}.{name}\n" in path.read_text()


def test_user_install_registers_ini_in_a_writable_xdg_data_dir(tmp_path):
    # GNOME Shell never reads ~/.local/share/gnome-shell/search-providers.
    import subprocess

    home, xdg = tmp_path / "home", tmp_path / "exports"
    home.mkdir()
    xdg.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_DATA_DIRS=f"/nonexistent:{xdg}:/usr/share")
    env.pop("XDG_DATA_HOME", None)
    env.pop("PROVIDERDIR", None)
    subprocess.run([str(ROOT / "install.sh")], check=True, env=env, capture_output=True)
    inis = [xdg / "gnome-shell" / "search-providers" / f"{provider.APP_ID}.{n}.ini"
            for n in SERVICE_NAMES]
    assert all(ini.exists() for ini in inis)
    share = home / ".local/share"
    for n in SERVICE_NAMES:
        assert (share / "applications" / f"{provider.APP_ID}.{n}.desktop").exists()
        assert (share / "icons/hicolor/scalable/apps" / f"{provider.APP_ID}.{n}.svg").exists()
    assert not (home / ".local/share/gnome-shell/search-providers").exists()
    service = home / ".local/share/dbus-1/services" / f"{provider.APP_ID}.service"
    assert f"Exec={home}/.local/bin/gnome-google-workspace-search" in service.read_text()

    subprocess.run([str(ROOT / "uninstall.sh")], check=True, env=env, capture_output=True)
    assert not any(ini.exists() for ini in inis)
    assert not service.exists()
    assert not list((share / "applications").glob("*.desktop"))
    assert not list((share / "icons/hicolor/scalable/apps").glob("*.svg"))


def test_user_install_without_writable_data_dir_explains_the_sudo_step(tmp_path):
    import subprocess

    if os.geteuid() == 0:
        pytest.skip("root can write everywhere")
    home, readonly = tmp_path / "home", tmp_path / "readonly"
    home.mkdir()
    readonly.mkdir()
    readonly.chmod(0o555)
    env = dict(os.environ, HOME=str(home), XDG_DATA_DIRS=f"{readonly}:/nonexistent")
    env.pop("XDG_DATA_HOME", None)
    env.pop("PROVIDERDIR", None)
    out = subprocess.run([str(ROOT / "install.sh")], check=True, env=env, capture_output=True, text=True)
    assert "ONE STEP LEFT" in out.stdout
    assert "sudo install" in out.stdout


# ---------------------------------------------------------------------------
# Login and account store
# ---------------------------------------------------------------------------


def write_client_secret(tmp_path, kind="installed"):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({kind: {
        "client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}))
    return path


def test_load_client_secret_accepts_desktop_and_web_clients(tmp_path):
    assert provider.load_client_secret(str(write_client_secret(tmp_path)))["kind"] == "installed"
    assert provider.load_client_secret(str(write_client_secret(tmp_path, "web")))["kind"] == "web"


def test_load_client_secret_errors_are_actionable(tmp_path):
    with pytest.raises(provider.LoginError, match="--client-secret"):
        provider.load_client_secret(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"installed": {"client_id": "x"}}))
    with pytest.raises(provider.LoginError, match="client_secret"):
        provider.load_client_secret(str(bad))


def test_pkce_challenge_is_s256_of_verifier():
    import base64
    import hashlib

    verifier, challenge = provider.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert 43 <= len(verifier) <= 128


def test_build_auth_url_requests_offline_access_with_pkce():
    from urllib.parse import parse_qs, urlparse

    client = {"client_id": "cid"}
    url = provider.build_auth_url(client, "http://127.0.0.1:1", provider.SCOPE_METADATA, "st", "ch")
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"]
    assert "consent" in q["prompt"][0]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == [provider.SCOPE_METADATA]
    assert q["redirect_uri"] == ["http://127.0.0.1:1"]


def fake_google(monkeypatch, email, calls):
    """Stand in for Google's token, about and revoke endpoints."""

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        calls.append((url, req.data))
        if url.startswith(provider.TOKEN_URL):
            return FakeResponse(json.dumps({
                "access_token": "at", "refresh_token": "rt-" + email, "expires_in": 3600,
                "scope": provider.SCOPE_METADATA,
            }).encode())
        if url.startswith("https://www.googleapis.com/drive/v3/about"):
            return FakeResponse(json.dumps({"user": {"emailAddress": email}}).encode())
        if url.startswith(provider.REVOKE_URL):
            return FakeResponse(b"{}")
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)


def fake_browser(monkeypatch, tamper_state=False):
    """Approve the consent screen: hit the loopback redirect like a browser would."""
    import http.client
    import threading
    from urllib.parse import parse_qs, urlparse

    def launch(url, _ctx):
        q = parse_qs(urlparse(url).query)
        target = urlparse(q["redirect_uri"][0])
        state = "evil" if tamper_state else q["state"][0]

        def visit():
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
            conn.request("GET", "/favicon.ico")
            conn.getresponse().read()
            conn.request("GET", f"/?state={state}&code=the-code")
            conn.getresponse().read()
            conn.close()

        threading.Thread(target=visit, daemon=True).start()

    monkeypatch.setattr(provider.Gio.AppInfo, "launch_default_for_uri", launch)


def test_login_stores_one_private_token_file_per_account(tmp_path, monkeypatch, capsys):
    accounts_dir, store = tmp_path / "accounts", tmp_path / "cfg" / "client_secret.json"
    secret = write_client_secret(tmp_path)
    fake_browser(monkeypatch)

    for email in ("me@gmail.com", "me@work.com"):
        calls = []
        fake_google(monkeypatch, email, calls)
        # The client secret is only needed the first time.
        first = email == "me@gmail.com"
        got = provider.login(str(secret) if first else None, timeout=10,
                             accounts_dir=str(accounts_dir), client_secret_store=str(store))
        assert got == email
        assert b"code_verifier=" in calls[0][1] and b"code=the-code" in calls[0][1]

    files = sorted(p.name for p in accounts_dir.iterdir())
    assert files == ["me@gmail.com.json", "me@work.com.json"]
    assert oct(accounts_dir.stat().st_mode & 0o777) == "0o700"
    for p in accounts_dir.iterdir():
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct(store.stat().st_mode & 0o777) == "0o600"
    saved = json.loads((accounts_dir / "me@work.com.json").read_text())
    assert saved["refresh_token"] == "rt-me@work.com" and saved["account"] == "me@work.com"

    found = provider.stored_accounts(str(accounts_dir))
    assert [a.identity for a in found] == ["me@gmail.com", "me@work.com"]
    assert all(a.source == "login" and a.email == a.identity for a in found)
    assert found[0].token() == "at"  # stored token is still valid, no refresh needed
    assert "Connected me@work.com" in capsys.readouterr().out


def test_login_rejects_a_forged_redirect(tmp_path, monkeypatch):
    fake_browser(monkeypatch, tamper_state=True)
    fake_google(monkeypatch, "x@y.z", [])
    with pytest.raises(provider.LoginError, match="state mismatch"):
        provider.login(str(write_client_secret(tmp_path)), timeout=10,
                       accounts_dir=str(tmp_path / "a"), client_secret_store=str(tmp_path / "s.json"))
    assert not (tmp_path / "a").exists()


def test_login_times_out_without_a_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(provider.Gio.AppInfo, "launch_default_for_uri", lambda url, ctx: None)
    with pytest.raises(provider.LoginError, match="timed out"):
        provider.login(str(write_client_secret(tmp_path)), timeout=0.3,
                       accounts_dir=str(tmp_path / "a"), client_secret_store=str(tmp_path / "s.json"))


def test_logout_revokes_and_removes_the_account(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    client = provider.load_client_secret(str(write_client_secret(tmp_path)))
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    provider.save_account("Me@Work.com", client, payload, provider.SCOPE_METADATA, str(accounts_dir))
    calls = []
    fake_google(monkeypatch, "me@work.com", calls)
    provider.logout("me@work.com", str(accounts_dir))
    assert calls[0][0] == provider.REVOKE_URL and b"token=rt" in calls[0][1]
    assert list(accounts_dir.iterdir()) == []
    with pytest.raises(provider.LoginError, match="no account"):
        provider.logout("me@work.com", str(accounts_dir))


def test_account_path_cannot_escape_the_accounts_dir(tmp_path):
    path = provider.account_path("../../etc/passwd@x", str(tmp_path))
    assert os.path.dirname(path) == str(tmp_path)
    assert "/" not in os.path.basename(path)


# ---------------------------------------------------------------------------
# Multiple accounts
# ---------------------------------------------------------------------------


def test_account_without_drive_scope_is_disabled_after_the_first_403():
    account = provider.Account("me@work.com", lambda: ("t", 3600), source="goa")
    body = b'{"error": {"message": "Request had insufficient authentication scopes."}}'

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(body))

    assert provider.drive_search(account, ["a"], dict(provider.DEFAULTS), opener=opener) == []
    assert account.disabled_services == {"drive"}


def test_other_403s_do_not_disable_the_account():
    account = make_account(["t"])

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"rate limit"))

    provider.drive_search(account, ["a"], dict(provider.DEFAULTS), opener=opener)
    assert not account.disabled_services


def test_accounts_merges_sources_skips_duplicates_and_remembers_disabled(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    client = provider.load_client_secret(str(write_client_secret(tmp_path)))
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    for email in ("me@gmail.com", "me@work.com"):
        provider.save_account(email, client, payload, provider.SCOPE_METADATA, str(accounts_dir))
    monkeypatch.setattr(provider, "ACCOUNTS_DIR", str(accounts_dir))
    goa = [provider.Account("me@work.com", lambda: ("g", 3600), source="goa"),
           provider.Account("other@old.com", lambda: ("g", 3600), source="goa")]
    monkeypatch.setattr(provider, "goa_accounts", lambda: list(goa))

    sp = provider.SearchProvider(GLib.MainLoop(), dict(provider.DEFAULTS))
    found = sp.accounts()
    # The GOA copy of an account we are logged in to is dropped.
    assert [(a.identity, a.source) for a in found] == [
        ("me@gmail.com", "login"), ("me@work.com", "login"), ("other@old.com", "goa"),
    ]
    found[2].disabled_services.add("drive")
    sp.invalidate_accounts()  # force a re-read, as happens every minute
    assert [a.identity for a in sp.accounts()] == ["me@gmail.com", "me@work.com"]


def test_search_all_queries_every_account_and_tags_results(monkeypatch):
    sp = provider.SearchProvider(GLib.MainLoop(), dict(provider.DEFAULTS))
    personal, work, broken = (
        provider.Account(email, lambda: ("t", 3600))
        for email in ("me@gmail.com", "me@work.com", "broken@x.com")
    )
    sp.manager._accounts, sp.manager._accounts_at = [personal, work, broken], float("inf")

    def fake_search(account, terms, cfg, **kw):
        if account is broken:
            raise OSError("network down")
        return [{"id": account.identity, "name": "f"}]

    monkeypatch.setattr(provider, "drive_search", fake_search)
    files = sp.search_all(["abc"])
    assert {(f["id"], f["_email"]) for f in files} == {
        ("me@gmail.com", "me@gmail.com"), ("me@work.com", "me@work.com"),
    }


def test_results_open_with_the_account_that_found_them(monkeypatch):
    sp = make_provider(monkeypatch, [])
    opened = []
    monkeypatch.setattr(sp, "_open", lambda url, email=None: opened.append(url))
    sp.files["1"] = {"id": "1", "webViewLink": "https://docs.google.com/d/1/edit?usp=drivesdk",
                     "_email": "me@work.com"}
    sp.ActivateResult(GLib.Variant("(sasu)", ("1", ["x"], 0)), FakeInvocation())
    assert opened == ["https://docs.google.com/d/1/edit?usp=drivesdk&authuser=me%40work.com"]
    assert provider.account_url("https://x/?authuser=0&a=1", "b@c.d") == "https://x/?a=1&authuser=b%40c.d"
    assert provider.account_url("https://x/", None) == "https://x/"


def test_multi_account_results_show_the_account(monkeypatch):
    sp = make_provider(monkeypatch, [])
    sp.lang = "en"
    f = {"id": "1", "name": "N", "mimeType": "application/pdf", "_account": "me@work.com"}
    assert sp.result_meta(f, True)["description"].get_string() == "PDF - me@work.com"
    assert sp.result_meta(f, False)["description"].get_string() == "PDF"


# ---------------------------------------------------------------------------
# Browser profiles
# ---------------------------------------------------------------------------


def write_local_state(home, relative=".config/google-chrome"):
    config_dir = home / relative
    config_dir.mkdir(parents=True)
    (config_dir / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"name": "Work", "user_name": "Me@Work.com"},
        "Profile 1": {"name": "Home", "user_name": "me@gmail.com"},
        "Profile 2": {"name": "Guest-like", "user_name": ""},
    }}}))
    return config_dir


def test_chromium_profiles_maps_signed_in_emails(tmp_path):
    config_dir = write_local_state(tmp_path)
    assert provider.chromium_profiles(str(config_dir)) == {
        "me@work.com": "Default", "me@gmail.com": "Profile 1",
    }
    assert provider.chromium_profiles(str(tmp_path / "nope")) == {}
    (config_dir / "Local State").write_text("not json")
    assert provider.chromium_profiles(str(config_dir)) == {}


def test_profile_command_for_chrome(tmp_path):
    write_local_state(tmp_path)
    argv = provider.profile_command(
        "google-chrome.desktop", "/usr/bin/google-chrome-stable %U", "https://x/1",
        "ME@gmail.com", home=str(tmp_path),
    )
    assert argv == ["/usr/bin/google-chrome-stable", "--profile-directory=Profile 1", "https://x/1"]


def test_profile_command_for_flatpak_chrome_strips_forwarding_markers(tmp_path):
    write_local_state(tmp_path, ".var/app/com.google.Chrome/config/google-chrome")
    cmd = "/usr/bin/flatpak run --branch=stable --command=/app/bin/chrome com.google.Chrome @@u %U @@"
    argv = provider.profile_command("com.google.Chrome.desktop", cmd, "https://x/1", "me@work.com",
                                    home=str(tmp_path))
    assert argv == ["/usr/bin/flatpak", "run", "--branch=stable", "--command=/app/bin/chrome",
                    "com.google.Chrome", "--profile-directory=Default", "https://x/1"]


def test_profile_command_falls_back_to_none(tmp_path):
    write_local_state(tmp_path)
    home = str(tmp_path)
    chrome = ("google-chrome.desktop", "/usr/bin/google-chrome-stable %U", "https://x/1")
    # Not a Chromium browser, unknown account, no account, no Local State.
    assert provider.profile_command("firefox.desktop", "firefox %u", "https://x/1", "me@work.com",
                                    home=home) is None
    assert provider.profile_command(*chrome, "stranger@x.com", home=home) is None
    assert provider.profile_command(*chrome, None, home=home) is None
    assert provider.profile_command(*chrome, "me@work.com", home=str(tmp_path / "empty")) is None


def test_profile_command_manual_override_wins(tmp_path):
    write_local_state(tmp_path)
    argv = provider.profile_command(
        "google-chrome.desktop", "/usr/bin/google-chrome-stable %U", "https://x/1", "me@work.com",
        overrides={"me@work.com": "Profile 7"}, home=str(tmp_path),
    )
    assert "--profile-directory=Profile 7" in argv


class FakeApp:
    def __init__(self, app_id, commandline):
        self._id, self._cmd = app_id, commandline

    def get_id(self):
        return self._id

    def get_commandline(self):
        return self._cmd


def patch_launchers(monkeypatch, app, popen_error=None):
    launched = {"popen": [], "default": []}

    def popen(argv, **kw):
        if popen_error:
            raise popen_error
        launched["popen"].append(argv)

    monkeypatch.setattr(provider.Gio.AppInfo, "get_default_for_uri_scheme", lambda scheme: app)
    monkeypatch.setattr(provider.subprocess, "Popen", popen)
    monkeypatch.setattr(provider.Gio.AppInfo, "launch_default_for_uri",
                        lambda url, ctx: launched["default"].append(url))
    return launched


def test_open_url_uses_the_profile_of_the_account(tmp_path, monkeypatch):
    write_local_state(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    launched = patch_launchers(monkeypatch, FakeApp("google-chrome.desktop", "/opt/chrome %U"))
    provider.open_url("https://x/1", "me@gmail.com", dict(provider.DEFAULTS))
    assert launched == {"popen": [["/opt/chrome", "--profile-directory=Profile 1", "https://x/1"]],
                        "default": []}


def test_open_url_falls_back_to_the_default_handler(tmp_path, monkeypatch):
    write_local_state(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    chrome = FakeApp("google-chrome.desktop", "/opt/chrome %U")
    cases = [
        (FakeApp("firefox.desktop", "firefox %u"), "me@gmail.com", dict(provider.DEFAULTS), None),
        (chrome, None, dict(provider.DEFAULTS), None),
        (chrome, "me@gmail.com", dict(provider.DEFAULTS, use_profiles=False), None),
        (chrome, "me@gmail.com", dict(provider.DEFAULTS), OSError("chrome is gone")),
        (None, "me@gmail.com", dict(provider.DEFAULTS), None),
    ]
    for app, email, cfg, error in cases:
        launched = patch_launchers(monkeypatch, app, popen_error=error)
        provider.open_url("https://x/1", email, cfg)
        assert launched == {"popen": [], "default": ["https://x/1"]}


def test_load_config_reads_browser_profile_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("GNOME_DRIVE_SEARCH_TOKEN_FILE", raising=False)
    ini = tmp_path / "config.ini"
    ini.write_text("[browser]\nuse_profiles = no\n[browser_profiles]\nMe@Work.com = Profile 3\n")
    cfg = provider.load_config(str(ini))
    assert cfg["use_profiles"] is False
    assert cfg["profiles"] == {"me@work.com": "Profile 3"}
    assert provider.load_config(str(tmp_path / "none.ini"))["use_profiles"] is True


def test_token_file_accounts_resolve_their_email_once(monkeypatch):
    calls = []

    def fake_fetch(token):
        calls.append(token)
        return "me@work.com"

    monkeypatch.setattr(provider, "fetch_email", fake_fetch)
    account = provider.Account("google_token.json", lambda: ("t", 3600), source="token_file")
    assert account.email is None
    assert account.resolve_email() == "me@work.com"
    assert account.resolve_email() == "me@work.com"
    assert calls == ["t"]
    # Accounts named after their email never need the extra request.
    assert provider.Account("a@b.c", lambda: ("t", 3600)).resolve_email() == "a@b.c"
    assert calls == ["t"]


def test_activate_result_passes_the_account_to_the_opener(monkeypatch):
    sp = make_provider(monkeypatch, [])
    seen = []
    monkeypatch.setattr(provider, "open_url", lambda url, email, cfg: seen.append((url, email)))
    sp.files["1"] = {"id": "1", "webViewLink": "https://d/1", "_email": "me@work.com"}
    sp.ActivateResult(GLib.Variant("(sasu)", ("1", ["x"], 0)), FakeInvocation())
    assert seen == [("https://d/1?authuser=me%40work.com", "me@work.com")]


# ---------------------------------------------------------------------------
# Guided setup
# ---------------------------------------------------------------------------

# Answers to step 2 of --setup without a key-driven terminal:
# Drive?, Contacts?, Gmail?, Calendar?, then Drive contents?
DRIVE_ONLY = ["", "n", "", "", ""]   # Contacts is on by default, so say no


def setup_env(tmp_path, monkeypatch, answers, registered=True):
    """Isolated config dir, scripted answers, a fake login and a fake Drive."""
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(provider, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(provider, "CONFIG_PATH", str(cfg_dir / "config.ini"))
    monkeypatch.setattr(provider, "ACCOUNTS_DIR", str(cfg_dir / "accounts"))
    monkeypatch.setattr(provider, "CLIENT_SECRET_PATH", str(cfg_dir / "client_secret.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(provider, "goa_accounts", lambda: [])
    monkeypatch.setattr(provider, "provider_registration",
                        lambda service=None: "/usr/share/p/x.ini" if registered else None)
    prompts, queue = PromptLog(), list(answers)

    def fake_input(prompt):
        prompts.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = queue.pop(0)
        return answer() if callable(answer) else answer

    monkeypatch.setattr("builtins.input", fake_input)
    opened = []
    monkeypatch.setattr(provider, "open_url", lambda url, email=None, cfg=None: opened.append((url, email)))
    monkeypatch.setattr(provider, "bundled_client_path", lambda: None)
    prompts.opened = opened
    logins = []
    emails = iter(["me@gmail.com", "me@work.com"])

    def fake_login(client_secret=None, scopes=None, login_hint=None, **kw):
        email = login_hint or next(emails)
        logins.append((email, scopes))
        client = provider.load_client_secret(provider.CLIENT_SECRET_PATH)
        payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        provider.save_account(email, client, payload, " ".join(scopes))
        return email

    monkeypatch.setattr(provider, "login", fake_login)
    monkeypatch.setattr(provider, "drive_search", lambda account, terms, cfg, **kw: [
        {"id": account.identity, "name": f"Budget of {account.identity}", "mimeType": "application/pdf",
         "modifiedTime": "2026-01-01T00:00:00Z"}])
    return cfg_dir, prompts, queue, logins


class PromptLog(list):
    """Prompts shown, plus the pages the setup opened in the browser."""

    opened = ()


def run_setup_with_config():
    return provider.run_setup(provider.load_config(provider.CONFIG_PATH), provider.CONFIG_PATH)


def connect(cfg_dir, email, *scopes):
    cfg_dir.mkdir(exist_ok=True)
    secret = cfg_dir / "client_secret.json"
    if not secret.exists():
        write_client_secret(cfg_dir)
    client = provider.load_client_secret(str(secret))
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    provider.save_account(email, client, payload, " ".join(scopes or [provider.SCOPE_METADATA]))


def test_setup_walks_a_new_user_through_client_two_accounts_and_a_test(tmp_path, monkeypatch, capsys):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    stale = write_named_client(downloads, "client_secret_old.json", "999")
    os.utime(stale, (1, 1))  # an old download from some other project: never offered

    def download():
        write_named_client(downloads, "client_secret_new.json", "555")
        return ""

    answers = DRIVE_ONLY + [
        "me@gmail.com",  # account that will own the OAuth client
        "", "",          # steps 1 and 2 done
        download,        # step 3 done: the browser saved the JSON
        "",              # path: accept the fresh download
        "",              # add an account? default yes
        "y",             # add another?
        "",              # add another? default no
        "budget",        # test search
    ]
    cfg_dir, prompts, queue, logins = setup_env(tmp_path, monkeypatch, answers)

    assert run_setup_with_config() == 0
    assert queue == []
    assert "client_secret_new.json" in prompts[9] and "old" not in prompts[9]
    pages = [url for url, _ in prompts.opened]
    assert "flows/enableapi?apiid=drive.googleapis.com" in pages[0]
    assert "/auth/overview" in pages[1] and "/auth/clients/create" in pages[2]
    assert all("authuser=me%40gmail.com" in url and who == "me@gmail.com" for url, who in prompts.opened)
    drive_only = [provider.SCOPE_EMAIL, provider.SCOPE_METADATA]
    assert logins == [("me@gmail.com", drive_only), ("me@work.com", drive_only)]
    stored = cfg_dir / "client_secret.json"
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    assert provider.load_client_secret(str(stored))["project"] == "proj-555"
    out = capsys.readouterr().out
    assert "OK: GNOME Shell can see the search sections" in out and "/usr/share/p)" in out
    assert "Audience: External" in out and "Publish app" in out and "Desktop app" in out
    assert "Google Drive: 2 result(s)" in out
    assert "Gmail:" not in out.split("5. Test")[1]  # disabled services are not searched


def test_client_creation_enables_the_apis_of_the_chosen_services():
    cfg = all_on()
    url = provider.creation_steps(cfg)[0][1]
    assert url.endswith("apiid=drive.googleapis.com,people.googleapis.com,"
                        "gmail.googleapis.com,calendar-json.googleapis.com")


def test_client_creation_rejects_web_clients_and_can_be_skipped(tmp_path, monkeypatch, capsys):
    web = write_client_secret(tmp_path, "web")
    _, prompts, queue, _ = setup_env(tmp_path, monkeypatch, ["", "", "", "", str(web), ""])
    #                        no owner: pages are printed, not opened / 3 steps / web client / stop
    assert provider.guide_client_creation(dict(provider.DEFAULTS)) is None
    assert queue == [] and list(prompts.opened) == []
    assert "needs a 'Desktop app' one" in capsys.readouterr().out


def test_a_bundled_client_makes_setup_skip_client_creation(tmp_path, monkeypatch, capsys):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    shipped = write_named_client(tmp_path / "share", "oauth_client.json", "777")
    monkeypatch.setattr(provider, "bundled_client_path", lambda: str(shipped))
    assert provider.default_client_path() == str(shipped)
    assert provider.setup_client(cfg=dict(provider.DEFAULTS)) is True
    assert "shipped with this install, Google Cloud project proj-777" in capsys.readouterr().out
    # The user's own client, once stored, wins over the bundled one.
    own = write_named_client(cfg_dir, "client_secret.json", "111")
    assert provider.default_client_path() == str(own)


def test_setup_is_rerunnable_and_offers_nothing_destructive(tmp_path, monkeypatch, capsys):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    connect(cfg_dir, "me@work.com")
    _, prompts, queue, logins = setup_env(tmp_path, monkeypatch, DRIVE_ONLY + ["", ""])
    #                                                      add another? no / skip the test
    assert run_setup_with_config() == 0
    assert queue == [] and logins == []
    out = capsys.readouterr().out
    assert "OAuth client: ready" in out
    assert "- me@work.com" in out
    assert "Add another account?" in prompts[5]


def test_setup_enabling_gmail_reauthorizes_existing_accounts(tmp_path, monkeypatch, capsys):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    connect(cfg_dir, "me@work.com")
    (cfg_dir / "config.ini").write_text("[search]\nmax_results = 4\n")
    answers = ["", "n", "y", "", "",  # Drive yes, Contacts no, Gmail YES, Calendar no, names only
               "",                    # authorize Gmail for me@work.com now? default yes
               "", ""]                # add another? no / skip the test
    _, prompts, queue, logins = setup_env(tmp_path, monkeypatch, answers)
    monkeypatch.setattr(provider.GmailService, "search", lambda self, account, terms, cfg: [])

    assert run_setup_with_config() == 0
    assert queue == []
    assert "has not authorized Gmail" in prompts[5]
    assert logins == [("me@work.com",
                       [provider.SCOPE_EMAIL, provider.SCOPE_METADATA, provider.SCOPE_GMAIL])]
    saved = provider.load_config(provider.CONFIG_PATH)
    assert saved["services"] == {"drive": True, "contacts": False, "gmail": True, "calendar": False}
    assert saved["max_results"] == 4  # keys the setup does not own survive
    assert "read access to ALL your mail" in capsys.readouterr().out


def test_setup_fulltext_is_saved_and_asks_for_read_access(tmp_path, monkeypatch):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    connect(cfg_dir, "old@work.com")
    answers = ["", "n", "", "", "y",  # Drive only, contents YES
               "n",                   # authorize now? no
               "", ""]
    _, prompts, queue, logins = setup_env(tmp_path, monkeypatch, answers)
    assert run_setup_with_config() == 1  # the only account cannot search contents yet
    assert "has not authorized Google Drive" in prompts[5] and logins == []
    assert provider.load_config(provider.CONFIG_PATH)["mode"] == "fulltext"


def test_setup_with_nothing_enabled_stops(tmp_path, monkeypatch, capsys):
    _, _, queue, logins = setup_env(tmp_path, monkeypatch, ["n", "n", "", ""])
    assert run_setup_with_config() == 1
    assert queue == [] and logins == []
    assert "Nothing enabled" in capsys.readouterr().out


def test_setup_without_a_client_stops_before_login(tmp_path, monkeypatch, capsys):
    answers = DRIVE_ONLY + ["", "", "", "", "/nope.json", ""]
    #                       no owner / 3 steps / bad path / give up
    _, _, queue, logins = setup_env(tmp_path, monkeypatch, answers, registered=False)
    assert run_setup_with_config() == 1
    assert queue == [] and logins == []
    out = capsys.readouterr().out
    assert "Not registered" in out and "Google Contacts" in out
    assert "no OAuth client found at /nope.json" in out
    assert "No usable account" in out


def test_setup_refuses_to_run_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: False, raising=False)
    assert provider.run_setup(dict(provider.DEFAULTS)) == 1
    assert "interactive" in capsys.readouterr().err


def test_setup_survives_ctrl_c(tmp_path, monkeypatch, capsys):
    setup_env(tmp_path, monkeypatch, [])

    def interrupt(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert run_setup_with_config() == 130
    assert "Run it again any time" in capsys.readouterr().out


def test_describe_account_mentions_the_browser_profile(tmp_path, monkeypatch):
    write_local_state(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    patch_launchers(monkeypatch, FakeApp("google-chrome.desktop", "/opt/chrome %U"))
    account = provider.Account("me@gmail.com", lambda: ("t", 3600))
    assert provider.describe_account(account, dict(provider.DEFAULTS)) == (
        "me@gmail.com  (opens in browser profile 'Profile 1')")
    legacy = provider.Account("token.json", lambda: ("t", 3600), source="token_file")
    assert provider.describe_account(legacy, dict(provider.DEFAULTS)) == "token.json  [token_file]"


def test_invalidate_accounts_rereads_even_right_after_boot(tmp_path, monkeypatch):
    # time.monotonic() starts near zero at boot; a forced refresh must not depend on it.
    monkeypatch.setattr(provider.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(provider, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setattr(provider, "goa_accounts", lambda: [])
    sp = provider.SearchProvider(GLib.MainLoop(), dict(provider.DEFAULTS))
    assert sp.accounts() == []
    client = provider.load_client_secret(str(write_client_secret(tmp_path)))
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    provider.save_account("new@x.com", client, payload, provider.SCOPE_METADATA)
    assert sp.accounts() == []  # still cached
    sp.invalidate_accounts()
    assert [a.identity for a in sp.accounts()] == ["new@x.com"]


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def all_on():
    cfg = dict(provider.DEFAULTS)
    cfg["services"] = dict.fromkeys(provider.DEFAULTS["services"], True)
    return cfg


def fake_api(monkeypatch, routes):
    """Answer api_get by URL substring; records the URLs requested."""
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        for fragment, answer in routes:
            if fragment in req.full_url:
                if isinstance(answer, Exception):
                    raise answer
                return FakeResponse(json.dumps(answer).encode())
        raise AssertionError(f"unexpected request: {req.full_url}")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    return seen


def http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))


def test_drive_and_contacts_are_enabled_by_default_and_config_can_change_it(tmp_path):
    defaults = provider.enabled_services(provider.load_config(str(tmp_path / "x")))
    assert [s.key for s in defaults] == ["drive", "contacts"]
    ini = tmp_path / "config.ini"
    ini.write_text("[services]\ndrive = no\ngmail = yes\ncontacts = no\n")
    cfg = provider.load_config(str(ini))
    assert [s.key for s in provider.enabled_services(cfg)] == ["gmail"]
    assert provider.DEFAULTS["services"]["gmail"] is False  # defaults are not mutated


def test_login_scopes_cover_exactly_the_enabled_services():
    cfg = dict(provider.DEFAULTS)
    assert provider.login_scopes(cfg) == [
        provider.SCOPE_EMAIL, provider.SCOPE_METADATA, provider.SCOPE_CONTACTS,
        provider.SCOPE_OTHER_CONTACTS, provider.SCOPE_DIRECTORY,
    ]
    cfg = all_on()
    cfg["mode"] = "fulltext"
    assert provider.login_scopes(cfg) == [
        provider.SCOPE_EMAIL, provider.SCOPE_READONLY, provider.SCOPE_CONTACTS,
        provider.SCOPE_OTHER_CONTACTS, provider.SCOPE_DIRECTORY, provider.SCOPE_GMAIL,
        provider.SCOPE_CALENDAR,
    ]


def test_a_service_only_searches_accounts_that_granted_it(monkeypatch):
    cfg = all_on()
    drive_only = provider.Account("a@x.com", lambda: ("t", 3600), scopes=[provider.SCOPE_METADATA])
    with_mail = provider.Account("b@x.com", lambda: ("t", 3600),
                                 scopes=[provider.SCOPE_READONLY, provider.SCOPE_GMAIL])
    unknown = provider.Account("token.json", lambda: ("t", 3600), source="token_file")
    manager = provider.AccountManager(cfg)
    manager._accounts, manager._accounts_at = [drive_only, with_mail, unknown], float("inf")

    def usable(key):
        sp = provider.SearchProvider(None, cfg, provider.SERVICES_BY_KEY[key], manager)
        return [a.identity for a in sp.accounts()]

    assert usable("drive") == ["a@x.com", "b@x.com", "token.json"]
    assert usable("gmail") == ["b@x.com", "token.json"]   # unknown scopes: try, then learn
    assert usable("calendar") == ["token.json"]
    cfg["mode"] = "fulltext"
    assert usable("drive") == ["b@x.com", "token.json"]   # names-only access is not enough
    cfg["services"]["gmail"] = False
    assert usable("gmail") == []


def test_api_get_disables_a_service_only_for_permanent_refusals():
    cases = [
        (403, "Request had insufficient authentication scopes.", True),
        (403, '{"error": {"status": "PERMISSION_DENIED", "reason": "SERVICE_DISABLED"}}', True),
        (403, "Gmail API has not been used in project 123 before or it is disabled.", True),
        (403, "User rate limit exceeded", False),
        (500, "backend error", False),
    ]
    for code, body, disabled in cases:
        account = make_account(["t"])
        result = provider.api_get(account, "https://x", "gmail",
                                  opener=lambda req, timeout=0, c=code, b=body: (_ for _ in ()).throw(
                                      http_error(c, b)))
        assert result is None
        assert ("gmail" in account.disabled_services) is disabled, body


def test_gmail_search_fetches_headers_groups_threads_and_builds_links(monkeypatch):
    seen = fake_api(monkeypatch, [
        ("/messages?", {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}),
        ("/messages/m1?", {"id": "m1", "threadId": "t1", "internalDate": "1767268800000",
                           "labelIds": ["UNREAD"], "payload": {"headers": [
                               {"name": "Subject", "value": "Invoice 42"},
                               {"name": "From", "value": "Ana Perez <ana@x.com>"}]}}),
        ("/messages/m2?", {"id": "m2", "threadId": "t1", "internalDate": "1767268900000",
                           "payload": {"headers": []}}),
        ("/messages/m3?", {"id": "m3", "threadId": "t3", "internalDate": "1767355200000",
                           "payload": {"headers": [{"name": "From", "value": "bot@x.com"}]}}),
    ])
    gmail = provider.SERVICES_BY_KEY["gmail"]
    items = gmail.sort(gmail.search(make_account(["t"] * 9), ["from:ana", "invoice"], all_on()))
    assert "q=from%3Aana+invoice" in seen[0]          # Gmail operators pass through untouched
    assert "format=metadata" in seen[1] and "metadataHeaders=Subject" in seen[1]
    assert [i["id"] for i in items] == ["t3", "t1"]   # newest first, one entry per thread
    assert gmail.meta(items[1], "en")[:2] == ("Invoice 42", ["Ana Perez", "2026-01-01"])
    assert gmail.meta(items[0], "es")[0] == "(sin asunto)"
    assert gmail.meta(items[0], "en")[1][0] == "bot@x.com"
    url = provider.account_url(gmail.url(items[1]), "me@work.com")
    assert url == "https://mail.google.com/mail/?authuser=me%40work.com#all/t1"
    assert gmail.search_url(["a b"]) == "https://mail.google.com/mail/#search/a%20b"


def test_gmail_search_with_no_matches_makes_one_request(monkeypatch):
    seen = fake_api(monkeypatch, [("/messages?", {"resultSizeEstimate": 0})])
    assert provider.SERVICES_BY_KEY["gmail"].search(make_account(["t"]), ["zzz"], all_on()) == []
    assert len(seen) == 1


def test_calendar_shows_upcoming_events_first_then_recent_past(monkeypatch):
    def event(eid, start, **extra):
        key = "dateTime" if "T" in start else "date"
        return {"id": eid, "summary": eid.title(), "start": {key: start},
                "htmlLink": f"https://calendar.google.com/event?eid={eid}", **extra}

    seen = fake_api(monkeypatch, [
        ("timeMax=", {"items": [event("older", "2026-08-01T10:00:00-03:00"),
                                event("recent", "2026-09-10")]}),
        ("timeMin=", {"items": [event("soon", "2026-09-25T15:30:00-03:00", location="Room 1"),
                                event("later", "2026-10-02T09:00:00-03:00")]}),
    ])
    calendar = provider.SERVICES_BY_KEY["calendar"]
    items = calendar.sort(calendar.search(make_account(["t"] * 4), ["sync"], all_on()))
    assert [i["id"] for i in items] == ["soon", "later", "recent", "older"]
    assert "singleEvents=true" in seen[0] and "orderBy=startTime" in seen[0] and "q=sync" in seen[0]
    assert calendar.meta(items[0], "en") == ("Soon", ["2026-09-25 15:30", "Room 1"], calendar.icon)
    assert calendar.meta(items[2], "en")[1] == ["2026-09-10", ""]  # all-day event
    assert calendar.url(items[0]).endswith("eid=soon")


def test_calendar_skips_the_past_when_upcoming_fills_the_page(monkeypatch):
    cfg = dict(all_on(), max_results=1)
    seen = fake_api(monkeypatch, [("timeMin=", {"items": [
        {"id": "e", "start": {"date": "2026-12-01"}}]})])
    assert len(provider.SERVICES_BY_KEY["calendar"].search(make_account(["t"]), ["x"], cfg)) == 1
    assert len(seen) == 1


def test_contacts_merges_own_contacts_and_directory(monkeypatch):
    def person(rid, name, mail, **extra):
        return {"resourceName": rid, "names": [{"displayName": name}],
                "emailAddresses": [{"value": mail}], **extra}

    ana = person("people/c1", "Ana Perez", "ana@work.com", phoneNumbers=[{"value": "+56 9 1"}],
                 organizations=[{"title": "CFO", "name": "Acme"}])
    seen = fake_api(monkeypatch, [
        ("people:searchContacts", {"results": [{"person": ana}]}),
        ("otherContacts:search", {"results": [
            {"person": person("otherContacts/c9", "Carla Soto", "carla@client.com")}]}),
        ("people:searchDirectoryPeople", {"people": [
            person("people/999", "Ana Perez", "ANA@work.com"),      # same person, from the directory
            person("people/777", "Bruno Diaz", "bruno@work.com")]}),
    ])
    contacts = provider.SERVICES_BY_KEY["contacts"]
    account = make_account(["t"] * 9)
    items = contacts.sort(contacts.search(account, ["an"], all_on()))
    assert [i["name"] for i in items] == ["Ana Perez", "Bruno Diaz", "Carla Soto"]
    assert seen[0].endswith("query=")                 # warm-up request, as Google asks
    assert contacts.url(items[2]) == "https://contacts.google.com/person/c9"
    assert contacts.meta(items[0], "en") == (
        "Ana Perez", ["ana@work.com", "+56 9 1", "CFO, Acme"], contacts.icon)
    assert contacts.url(items[0]) == "https://contacts.google.com/person/c1"
    contacts.search(account, ["br"], all_on())
    assert sum(u.endswith("query=") for u in seen) == 2  # one warm-up per searchable source, once


def test_contacts_stops_asking_for_a_directory_that_does_not_exist(monkeypatch):
    seen = fake_api(monkeypatch, [
        ("people:searchContacts", {"results": []}),
        ("otherContacts:search", {"results": []}),
        ("people:searchDirectoryPeople", http_error(400, "Must be a G Suite domain user.")),
    ])
    contacts = provider.SERVICES_BY_KEY["contacts"]
    account = make_account(["t"] * 9)
    assert contacts.search(account, ["ana"], all_on()) == []
    assert account.disabled_services == {"directory"}
    assert contacts.usable(account, all_on())  # own contacts keep working
    before = len(seen)
    contacts.search(account, ["ana"], all_on())
    assert not any("Directory" in u for u in seen[before:])
    # Accounts that never granted the optional scopes are not asked for them at all.
    personal = provider.Account("me@gmail.com", lambda: ("t", 3600), scopes=[provider.SCOPE_CONTACTS])
    before = len(seen)
    contacts.search(personal, ["ana"], all_on())
    assert all("people:searchContacts" in u for u in seen[before:])


def test_each_provider_answers_dbus_with_its_own_service(monkeypatch):
    cfg = dict(all_on(), debounce_ms=1)
    manager = provider.AccountManager(cfg)
    manager._accounts, manager._accounts_at = [make_account(["t"] * 9)], float("inf")
    gmail = provider.SearchProvider(GLib.MainLoop(), cfg, provider.SERVICES_BY_KEY["gmail"], manager)
    gmail.lang = "en"
    monkeypatch.setattr(provider.GmailService, "search", lambda self, account, terms, cfg: [
        {"id": "t1", "subject": "Hello", "from": "Ana <a@x.com>", "date": 1767268800000}])
    inv = FakeInvocation()
    gmail.handle_call(None, None, None, None, "GetInitialResultSet", GLib.Variant("(as)", (["hello"],)), inv)
    pump()
    assert inv.value == (["t1"],)
    metas = FakeInvocation()
    gmail.GetResultMetas(GLib.Variant("(as)", (["t1"],)), metas)
    assert metas.value[0][0] == {"id": "t1", "name": "Hello", "description": "Ana - 2026-01-01",
                                 "gicon": f"{provider.APP_ID}.Gmail"}
    opened = []
    monkeypatch.setattr(gmail, "_open", lambda url, email=None: opened.append(url))
    gmail.ActivateResult(GLib.Variant("(sasu)", ("t1", [], 0)), FakeInvocation())
    gmail.LaunchSearch(GLib.Variant("(asu)", (["hello"], 0)), FakeInvocation())
    assert opened == ["https://mail.google.com/mail/?authuser=me%40example.com#all/t1",
                      "https://mail.google.com/mail/#search/hello"]


def test_disabled_service_answers_immediately_with_nothing(monkeypatch):
    cfg = dict(provider.DEFAULTS, services=dict(provider.DEFAULTS["services"]))
    manager = provider.AccountManager(cfg)
    manager._accounts, manager._accounts_at = [make_account(["t"])], float("inf")
    gmail = provider.SearchProvider(None, cfg, provider.SERVICES_BY_KEY["gmail"], manager)
    monkeypatch.setattr(provider.GmailService, "search",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not search")))
    inv = FakeInvocation()
    gmail.GetInitialResultSet(GLib.Variant("(as)", (["hello"],)), inv)
    assert inv.value == ([],)


def test_results_are_capped_after_merging_accounts(monkeypatch):
    sp = make_provider(monkeypatch, [{"id": str(i), "name": "f", "modifiedTime": f"2026-01-{i:02d}"}
                                     for i in range(1, 9)], max_results=3)
    inv = FakeInvocation()
    sp.GetInitialResultSet(GLib.Variant("(as)", (["abc"],)), inv)
    pump()
    assert inv.value == (["8", "7", "6"],)


def test_fetch_email_falls_back_across_apis(monkeypatch):
    fake_api(monkeypatch, [
        ("drive/v3/about", http_error(403, "insufficient")),
        ("/profile", {"emailAddress": "me@gmail.com"}),
    ])
    assert provider.fetch_email("token") == "me@gmail.com"
    fake_api(monkeypatch, [("drive/v3/about", http_error(403, "x")), ("/profile", http_error(403, "x")),
                           ("userinfo", http_error(401, "x"))])
    with pytest.raises(provider.LoginError, match="which account"):
        provider.fetch_email("token")


def test_accounts_listing_says_what_each_account_can_search(tmp_path, monkeypatch, capsys):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    connect(cfg_dir, "a@x.com")
    connect(cfg_dir, "b@x.com", provider.SCOPE_METADATA, provider.SCOPE_GMAIL)
    cfg = provider.load_config(provider.CONFIG_PATH)
    cfg["services"]["gmail"] = True
    assert provider.run_accounts(cfg) == 0
    out = capsys.readouterr().out
    assert ("a@x.com  (--login)  searches: Google Drive\n"
            "    not authorized for: Google Contacts, Gmail") in out
    assert "b@x.com  (--login)  searches: Google Drive, Gmail\n" in out


def test_legacy_config_dir_is_migrated_once(tmp_path, monkeypatch):
    old, new = tmp_path / "gnome-drive-search-provider", tmp_path / "gnome-google-workspace-search"
    (old / "accounts").mkdir(parents=True)
    (old / "accounts" / "me@x.com.json").write_text("{}")
    monkeypatch.setattr(provider, "LEGACY_CONFIG_DIR", str(old))
    monkeypatch.setattr(provider, "CONFIG_DIR", str(new))
    provider.migrate_legacy_config()
    assert (new / "accounts" / "me@x.com.json").exists() and not old.exists()
    old.mkdir()
    (old / "stale").write_text("")
    provider.migrate_legacy_config()  # never overwrites the new directory
    assert not (new / "stale").exists() and old.exists()


# ---------------------------------------------------------------------------
# More than one OAuth client (an Internal work client plus a personal one)
# ---------------------------------------------------------------------------


def write_named_client(directory, name, project):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({"installed": {
        "client_id": f"{project}-abc.apps.googleusercontent.com", "client_secret": f"s-{project}",
        "project_id": f"proj-{project}", "token_uri": "https://oauth2.googleapis.com/token"}}))
    return path


def test_login_with_another_client_never_replaces_the_default(tmp_path, monkeypatch):
    store = write_named_client(tmp_path / "cfg", "client_secret.json", "111")
    other = write_named_client(tmp_path / "Downloads", "client_secret_222.json", "222")
    fake_browser(monkeypatch)
    fake_google(monkeypatch, "me@gmail.com", [])
    provider.login(str(other), timeout=10, accounts_dir=str(tmp_path / "cfg" / "accounts"),
                   client_secret_store=str(store))
    assert provider.load_client_secret(str(store))["project"] == "proj-111"
    kept = tmp_path / "cfg" / "clients" / "proj-222.json"
    assert kept.exists() and oct(kept.stat().st_mode & 0o777) == "0o600"
    saved = json.loads((tmp_path / "cfg" / "accounts" / "me@gmail.com.json").read_text())
    assert saved["client_id"].startswith("222-")  # the token refreshes with its own client
    known = provider.known_clients(str(store), str(tmp_path / "cfg" / "clients"))
    assert [c["project"] for c, _ in known] == ["proj-111", "proj-222"]


def test_setup_offers_another_client_when_google_refuses_the_account(tmp_path, monkeypatch, capsys):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    write_named_client(cfg_dir, "client_secret.json", "111")
    downloads = tmp_path / "Downloads"
    write_named_client(downloads, "client_secret_old.json", "999")  # must never be guessed

    def download():
        write_named_client(downloads, "client_secret_new.json", "222")
        return ""

    answers = DRIVE_ONLY + [
        "",              # add an account? default yes
        "",              # register another client now? default yes
        "me@gmail.com",  # owner of the new client
        "", "", download,
        "",              # path: accept the fresh download
        "",              # add another? no
        "",              # skip the test
    ]
    _, prompts, queue, _ = setup_env(tmp_path, monkeypatch, answers)
    monkeypatch.setattr(provider, "CLIENTS_DIR", str(cfg_dir / "clients"))
    attempts = []

    def picky_login(client_secret=None, scopes=None, client=None, **kw):
        attempts.append(client_secret)
        if client_secret is None:
            raise KeyboardInterrupt  # the person saw org_internal and pressed Ctrl+C
        chosen = provider.load_client_secret(client_secret)
        payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        provider.save_account("me@gmail.com", chosen, payload, " ".join(scopes))
        return "me@gmail.com"

    monkeypatch.setattr(provider, "login", picky_login)
    assert run_setup_with_config() == 0
    assert queue == []
    kept = str(cfg_dir / "clients" / "proj-222.json")
    assert attempts == [None, kept]  # stored under clients/, the default is untouched
    assert provider.load_client_secret(str(cfg_dir / "client_secret.json"))["project"] == "proj-111"
    out = capsys.readouterr().out
    assert "Login cancelled." in out
    assert "org_internal" in out and "project proj-111 is Internal" in out
    assert "client_secret_old.json" not in out + "".join(prompts)
    assert "+ me@gmail.com" in out


def test_setup_reauthorizes_an_account_with_the_client_it_was_connected_with(tmp_path, monkeypatch):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    write_named_client(cfg_dir, "client_secret.json", "111")
    personal = provider.load_client_secret(
        str(write_named_client(tmp_path / "elsewhere", "c.json", "222")))
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    provider.save_account("me@gmail.com", personal, payload, provider.SCOPE_METADATA)
    answers = ["", "", "y", "", "",  # enable Gmail
               "",                   # authorize it for me@gmail.com now? yes
               "", ""]
    _, _, queue, _ = setup_env(tmp_path, monkeypatch, answers)
    used = []

    def recording_login(client_secret=None, scopes=None, client=None, login_hint=None, **kw):
        used.append((login_hint, client and client["client_id"]))
        provider.save_account(login_hint, client, payload, " ".join(scopes))
        return login_hint

    monkeypatch.setattr(provider, "login", recording_login)
    monkeypatch.setattr(provider.GmailService, "search", lambda self, account, terms, cfg: [])
    assert run_setup_with_config() == 0
    assert used == [("me@gmail.com", "222-abc.apps.googleusercontent.com")]


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------

ITEMS = [("drive", "Google Drive", "names"), ("gmail", "Gmail", "ALL your mail"),
         ("calendar", "Google Calendar", "events")]
UP, DOWN = "\x1b[A", "\x1b[B"


def run_checklist(keys, checked=("drive",), width=80):
    out = io.StringIO()
    chosen = provider.checklist(ITEMS, checked, keys=iter(keys).__next__, out=out, width=width)
    return chosen, out.getvalue()


def test_checklist_lines_show_marks_cursor_and_aligned_notes():
    assert provider.checklist_lines(ITEMS, {"gmail"}, 1) == [
        "    [ ] Google Drive     names",
        "  > [x] Gmail            ALL your mail",
        "    [ ] Google Calendar  events",
    ]
    assert provider.checklist_lines(ITEMS, set(), 0, width=24)[0] == "  > [ ] Google Drive   "


def test_checklist_moves_toggles_and_confirms():
    chosen, out = run_checklist([DOWN, " ", DOWN, "x", "\r"])
    assert chosen == {"drive", "gmail", "calendar"}
    assert "space or x marks" in out
    assert out.count("\x1b[3A") == 4          # redrawn in place after each key, not scrolled
    chosen, _ = run_checklist([" ", "\r"])     # unmark the only one
    assert chosen == set()


def test_checklist_wraps_around_and_supports_vim_keys_and_all():
    assert run_checklist([UP, " ", "\r"])[0] == {"drive", "calendar"}   # up from the top wraps
    assert run_checklist(["j", "j", "j", "x", "\n"])[0] == set()         # wraps back to Drive
    assert run_checklist(["a", "\r"])[0] == {"drive", "gmail", "calendar"}
    assert run_checklist(["a", "a", "\r"])[0] == set()
    assert run_checklist(["?", "\x1b", "q", "\r"])[0] == {"drive"}       # unknown keys do nothing


def test_checklist_is_not_used_without_a_real_terminal(monkeypatch):
    assert provider.checklist_available() is False  # pytest's stdin is not a terminal
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(provider.os, "isatty", lambda fd: True)
    assert provider.checklist_available() is False


def test_setup_picks_services_from_the_checklist_when_the_terminal_allows(tmp_path, monkeypatch, capsys):
    cfg_dir, *_ = setup_env(tmp_path, monkeypatch, [])
    connect(cfg_dir, "me@work.com", provider.SCOPE_METADATA, provider.SCOPE_GMAIL, provider.SCOPE_CONTACTS)
    _, prompts, queue, _ = setup_env(tmp_path, monkeypatch, ["", "", ""])
    #                        Drive contents? no / add another account? no / skip the test
    monkeypatch.setattr(provider, "checklist_available", lambda: True)
    monkeypatch.setattr(provider, "read_key", iter([DOWN, DOWN, " ", "\r"]).__next__)   # mark Gmail, third
    monkeypatch.setattr(provider.GmailService, "search", lambda self, account, terms, cfg: [])
    monkeypatch.setattr(provider.ContactsService, "search", lambda self, account, terms, cfg: [])

    assert run_setup_with_config() == 0
    assert queue == []
    assert not any("Search Gmail?" in p for p in prompts)      # no question per service
    saved = provider.load_config(provider.CONFIG_PATH)["services"]
    assert saved == {"drive": True, "gmail": True, "calendar": False, "contacts": True}
    out = capsys.readouterr().out
    assert "[x] Gmail" in out and "[x] Google Contacts" in out and "needs to read all your mail" in out
    assert "search mail without being able to read it" in out  # explained while highlighted
    assert "Selected: Google Drive, Google Contacts, Gmail." in out


def test_checklist_on_a_real_terminal_keeps_every_key_of_a_burst():
    # Regression: switching terminal modes on every key discarded the pending input,
    # dropping keys while an arrow was held down. Drive the real termios path in a pty.
    import pty
    import select
    import time

    program = (
        "import importlib.util, importlib.machinery as m\n"
        f"l = m.SourceFileLoader('p', {str(SCRIPT)!r}); s = importlib.util.spec_from_loader('p', l)\n"
        "p = importlib.util.module_from_spec(s); l.exec_module(p)\n"
        "items = [('a', 'A', ''), ('b', 'B', ''), ('c', 'C', ''), ('d', 'D', '')]\n"
        "print('CHOSEN', sorted(p.checklist(items, set())))\n"
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm"
        os.execv(sys.executable, [sys.executable, "-c", program])
    output = b""

    def read_until(marker, seconds):
        nonlocal output
        deadline = time.time() + seconds
        while marker not in output and time.time() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    output += os.read(fd, 65536)
                except OSError:
                    break
        return marker in output

    assert read_until(b"Enter confirms", 20), output
    time.sleep(0.2)
    os.write(fd, b"x\x1b[Bx\x1b[B\x1b[Bx\r")  # mark a, b and d, all in one burst
    assert read_until(b"CHOSEN", 10), output
    os.waitpid(pid, 0)
    assert b"CHOSEN ['a', 'b', 'd']" in output


def test_checklist_explains_the_highlighted_row_and_still_redraws_in_place():
    details = {"drive": "Only names.", "gmail": "word " * 60}
    out = io.StringIO()
    provider.checklist(ITEMS, {"drive"}, keys=iter([DOWN, DOWN, "\r"]).__next__, out=out, width=60,
                       details=details)
    text = out.getvalue()
    assert "Only names." in text
    assert text.count("\x1b[6A") == 2  # 3 rows + blank + 2 detail lines, redrawn after each move
    long_lines = provider.detail_lines(details["gmail"], 60)
    assert len(long_lines) == provider.DETAIL_LINES and long_lines[-1].endswith("...")
    assert all(len(line) <= 59 for line in long_lines)
    assert provider.detail_lines("", 60) == ["    ", "    "]  # rows without a detail keep the height


def test_every_service_has_a_short_note_and_an_explanation_that_fits():
    for service in provider.SERVICES:
        assert service.short and service.detail
        row = provider.checklist_lines([(service.key, "Google Contacts", service.short)], set(), 0)[0]
        assert len(row) < 80
        shown = provider.detail_lines(service.detail, 80)
        assert not shown[-1].endswith("..."), service.key  # fits in an 80-column terminal
