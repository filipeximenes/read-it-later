"""Tests for reaching a hosted instance: URL rules, token storage, failures."""

from __future__ import annotations

import json
import stat

import httpx
import pytest

from ril import config as config_module
from ril import remote as remote_module
from ril.remote import (
    Remote,
    RemoteAuthError,
    RemoteClient,
    RemoteError,
    RemoteNotConfiguredError,
    RemoteProtocolError,
    RemoteStatus,
    RemoteUnavailableError,
    forget_token,
    load_remote,
    load_token,
    normalise_url,
    save_token,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep every test off the real config directory and environment.

    Both modules have to be redirected. `ril.config` resolves its paths at
    import time from the home directory, so patching only `ril.remote` would
    leave these tests reading — and able to write — the developer's own
    settings, and passing or failing on what happens to be in them.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setattr(remote_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(remote_module, "CREDENTIALS_FILE", config_dir / "credentials.json")
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.delenv("RIL_SYNC_TOKEN", raising=False)
    monkeypatch.delenv("RIL_SYNC_URL", raising=False)
    return config_dir


# --- URL rules --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/ril", "https://example.com/ril"),
        # A trailing slash would double up when a path is appended.
        ("https://example.com/ril/", "https://example.com/ril"),
        ("  https://example.com  ", "https://example.com"),
        # Loopback never leaves the machine, so plain http is allowed there.
        ("http://localhost:8484", "http://localhost:8484"),
        ("http://127.0.0.1:8484/ril", "http://127.0.0.1:8484/ril"),
    ],
)
def test_a_usable_url_is_accepted_and_tidied(raw, expected):
    assert normalise_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # A bearer token over plain http can be read in transit.
        "http://example.com/ril",
        "http://192.168.1.10/ril",
        "ftp://example.com",
        "example.com/ril",
        "",
        "   ",
    ],
)
def test_an_unsafe_or_malformed_url_is_refused(raw):
    with pytest.raises(RemoteError):
        normalise_url(raw)


def test_the_refusal_says_how_to_fix_a_missing_scheme():
    with pytest.raises(RemoteError, match="https://example.com"):
        normalise_url("example.com")


def test_an_endpoint_is_joined_without_doubling_slashes():
    remote = Remote(url="https://example.com/ril", token="t")
    assert remote.endpoint("/api/sync") == "https://example.com/ril/api/sync"
    assert remote.endpoint("api/sync") == "https://example.com/ril/api/sync"


# --- token storage ----------------------------------------------------------


def test_a_stored_token_comes_back():
    save_token("secret-token")
    assert load_token() == "secret-token"


def test_the_token_file_is_readable_by_its_owner_only(_isolate):
    save_token("secret-token")
    credentials = _isolate / "credentials.json"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert stat.S_IMODE(_isolate.stat().st_mode) == 0o700


def test_no_token_is_not_an_error():
    assert load_token() is None


def test_the_environment_overrides_a_stored_token(monkeypatch):
    save_token("stored")
    monkeypatch.setenv("RIL_SYNC_TOKEN", "from-env")
    assert load_token() == "from-env"


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"sync_token": null}', "{}"])
def test_a_damaged_credentials_file_reads_as_absent(_isolate, content):
    """A broken file must not crash a command that only wanted to try syncing."""
    _isolate.mkdir(parents=True, exist_ok=True)
    (_isolate / "credentials.json").write_text(content)
    assert load_token() is None


def test_saving_a_token_leaves_other_keys_alone(_isolate):
    save_token("first")
    credentials = _isolate / "credentials.json"
    stored = json.loads(credentials.read_text())
    stored["something_else"] = "keep me"
    credentials.write_text(json.dumps(stored))

    save_token("second")

    stored = json.loads(credentials.read_text())
    assert stored["sync_token"] == "second"
    assert stored["something_else"] == "keep me"


def test_forgetting_removes_the_token():
    save_token("secret")
    assert forget_token() is True
    assert load_token() is None
    # Saying so twice is not an error, it is just nothing to do.
    assert forget_token() is False


def test_forgetting_keeps_any_other_credential(_isolate):
    save_token("secret")
    credentials = _isolate / "credentials.json"
    stored = json.loads(credentials.read_text())
    stored["other"] = "kept"
    credentials.write_text(json.dumps(stored))

    forget_token()

    assert json.loads(credentials.read_text()) == {"other": "kept"}


# --- what is configured -----------------------------------------------------


def test_an_unconfigured_remote_says_what_to_run():
    with pytest.raises(RemoteNotConfiguredError, match="ril sync login"):
        load_remote()


def test_a_url_with_no_token_names_the_missing_half(monkeypatch):
    monkeypatch.setenv("RIL_SYNC_URL", "https://example.com/ril")
    with pytest.raises(RemoteNotConfiguredError, match="token"):
        load_remote()


def test_a_token_with_no_url_names_the_missing_half(monkeypatch):
    monkeypatch.setenv("RIL_SYNC_TOKEN", "t")
    with pytest.raises(RemoteNotConfiguredError, match="URL"):
        load_remote()


def test_a_configured_remote_loads(monkeypatch):
    monkeypatch.setenv("RIL_SYNC_URL", "https://example.com/ril/")
    monkeypatch.setenv("RIL_SYNC_TOKEN", "t")
    remote = load_remote()
    assert remote.url == "https://example.com/ril"
    assert remote.token == "t"


# --- the client -------------------------------------------------------------


def _client(handler, token="t"):
    remote = Remote(url="https://example.com/ril", token=token)
    client = RemoteClient(remote)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        headers=dict(client._client.headers),
    )
    return client


def test_the_token_travels_in_the_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    _client(handler, token="secret-token").request("GET", "/api/sync")
    assert seen["auth"] == "Bearer secret-token"
    assert seen["url"] == "https://example.com/ril/api/sync"
    # Never in the query string, where it would land in an access log.
    assert "secret-token" not in seen["url"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_told_apart_from_an_outage(status):
    client = _client(lambda request: httpx.Response(status))
    with pytest.raises(RemoteAuthError, match="ril sync login"):
        client.request("GET", "/api/sync")


def test_a_redirect_to_login_is_an_auth_failure_not_a_follow():
    """The gateway redirects a browser to its login page.

    Following that would post the body to a login form, so it has to be an
    error rather than something the client chases.
    """
    client = _client(
        lambda request: httpx.Response(303, headers={"location": "/_hub/login?next=/ril"})
    )
    with pytest.raises(RemoteAuthError, match="_hub/login"):
        client.request("POST", "/api/sync")


def test_an_unreachable_server_is_transient():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(RemoteUnavailableError, match="Could not reach"):
        _client(handler).request("GET", "/api/sync")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_throttling_and_server_faults_are_transient(status):
    client = _client(lambda request: httpx.Response(status))
    with pytest.raises(RemoteUnavailableError):
        client.request("GET", "/api/sync")


def test_a_probe_finds_a_ready_server():
    client = _client(lambda request: httpx.Response(200, json={"cursor": 0, "changes": []}))
    assert client.probe() is RemoteStatus.READY


def test_a_probe_reports_a_server_without_sync_endpoints():
    """404 means the gateway authorised us and the instance behind it is older."""
    client = _client(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
    assert client.probe() is RemoteStatus.NO_SYNC_ENDPOINT


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_probe_treats_a_server_fault_as_authorised_but_down(status):
    """The gateway refuses an unknown token before it proxies anything.

    So a 5xx can only come from after it let us in: the credential is good
    and the library behind it is down. Refusing to store a token we have just
    proven works would be wrong.
    """
    client = _client(lambda request: httpx.Response(status))
    assert client.probe() is RemoteStatus.UPSTREAM_DOWN


def test_a_probe_still_refuses_an_unreachable_server():
    """Nothing was learned about the token here, so this is not a pass."""

    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(RemoteUnavailableError):
        _client(handler).probe()


@pytest.mark.parametrize("status", [401, 403])
def test_a_probe_reports_a_refused_token(status):
    client = _client(lambda request: httpx.Response(status))
    with pytest.raises(RemoteAuthError):
        client.probe()


def test_a_probe_rejects_an_answer_it_cannot_read():
    client = _client(lambda request: httpx.Response(418))
    with pytest.raises(RemoteProtocolError):
        client.probe()
