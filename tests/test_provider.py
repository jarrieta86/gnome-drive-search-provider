import importlib.util
import io
import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "gnome-drive-search-provider.py"

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
    sp._accounts = [make_account(["t"] * 10)]
    sp._accounts_at = float("inf")
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
    monkeypatch.setattr(sp, "_open", lambda url: opened.append(url))
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
    ini = (conf / f"{provider.APP_ID}.ini").read_text()
    assert f"BusName={provider.BUS_NAME}" in ini
    assert f"ObjectPath={provider.OBJECT_PATH}" in ini
    assert f"DesktopId={provider.APP_ID}.desktop" in ini
    assert (conf / f"{provider.APP_ID}.desktop").exists()
    assert f"Name={provider.BUS_NAME}" in (conf / f"{provider.APP_ID}.service.in").read_text()
    assert os.access(SCRIPT, os.X_OK)


def test_desktop_file_is_accepted_by_gnome_shell(monkeypatch):
    # GNOME Shell drops providers whose desktop file fails should_show(), which
    # is the case with NoDisplay=true or when OnlyShowIn excludes GNOME.
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    path = ROOT / "conf" / f"{provider.APP_ID}.desktop"
    assert "NoDisplay" not in path.read_text()
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("GioUnix", "2.0")
        from gi.repository import GioUnix
        info = GioUnix.DesktopAppInfo.new_from_filename(str(path))
    except (ValueError, ImportError):
        info = provider.Gio.DesktopAppInfo.new_from_filename(str(path))
    assert info is not None
    assert info.should_show()


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
    ini = xdg / "gnome-shell" / "search-providers" / f"{provider.APP_ID}.ini"
    assert ini.exists()
    assert not (home / ".local/share/gnome-shell/search-providers").exists()
    service = home / ".local/share/dbus-1/services" / f"{provider.APP_ID}.service"
    assert f"Exec={home}/.local/bin/gnome-drive-search-provider" in service.read_text()

    subprocess.run([str(ROOT / "uninstall.sh")], check=True, env=env, capture_output=True)
    assert not ini.exists()
    assert not service.exists()


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
    assert account.disabled


def test_other_403s_do_not_disable_the_account():
    account = make_account(["t"])

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"rate limit"))

    provider.drive_search(account, ["a"], dict(provider.DEFAULTS), opener=opener)
    assert not account.disabled


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
    found[2].disabled = True
    sp._accounts_at = 0  # force a re-read, as happens every minute
    assert [a.identity for a in sp.accounts()] == ["me@gmail.com", "me@work.com"]


def test_search_all_queries_every_account_and_tags_results(monkeypatch):
    sp = provider.SearchProvider(GLib.MainLoop(), dict(provider.DEFAULTS))
    personal, work = make_account(["t"]), make_account(["t"])
    personal.identity, work.identity = "me@gmail.com", "me@work.com"
    broken = make_account(["t"])
    broken.identity = "broken@x.com"
    sp._accounts, sp._accounts_at = [personal, work, broken], float("inf")

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
    monkeypatch.setattr(sp, "_open", lambda url: opened.append(url))
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
