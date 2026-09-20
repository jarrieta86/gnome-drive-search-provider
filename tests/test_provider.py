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
