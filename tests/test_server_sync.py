"""Tests for starting a sync from the web UI.

The exchange itself is covered in `test_sync.py`; these cover the two things
the page needs: being told whether a sync button makes sense here, and a way
to start one that a cross-origin page cannot.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ril import config as config_module
from ril import remote as remote_module
from ril import server as server_module
from ril.remote import RemoteUnavailableError
from ril.server import build_app
from ril.sync import SyncReport


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep every test off the real config directory and environment."""
    config_dir = tmp_path / "config"
    monkeypatch.setattr(remote_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(remote_module, "CREDENTIALS_FILE", config_dir / "credentials.json")
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.delenv("RIL_SYNC_TOKEN", raising=False)
    monkeypatch.delenv("RIL_SYNC_URL", raising=False)


@pytest.fixture
def client(tmp_path):
    return TestClient(build_app(tmp_path / "data"))


def _configure_sync(monkeypatch):
    monkeypatch.setenv("RIL_SYNC_URL", "https://example.com/ril")
    monkeypatch.setenv("RIL_SYNC_TOKEN", "s3cret")


def test_config_says_disabled_without_a_remote(client):
    # The hosted copy runs this same app and has nowhere of its own to sync
    # to, so its page must not offer a button.
    assert client.get("/api/sync/config").json() == {"enabled": False}


def test_config_says_enabled_once_a_remote_is_set(client, monkeypatch):
    _configure_sync(monkeypatch)
    assert client.get("/api/sync/config").json() == {"enabled": True}


def test_config_never_reveals_the_credential(client, monkeypatch):
    _configure_sync(monkeypatch)
    body = client.get("/api/sync/config").text
    assert "s3cret" not in body
    assert "example.com" not in body


def test_running_a_sync_needs_the_header(client, monkeypatch):
    _configure_sync(monkeypatch)
    called = False

    def _never(*args, **kwargs):
        nonlocal called
        called = True
        return SyncReport()

    monkeypatch.setattr(server_module, "_sync_with_remote", _never)
    response = client.post("/api/sync/run")
    assert response.status_code == 403
    assert called is False


def test_running_a_sync_reports_what_moved(client, monkeypatch):
    _configure_sync(monkeypatch)
    seen = {}

    def _fake(data_folder, remote):
        seen["url"] = remote.url
        return SyncReport(sent=2, received=3, bodies_sent=1, bodies_received=4)

    monkeypatch.setattr(server_module, "_sync_with_remote", _fake)
    response = client.post("/api/sync/run", headers={"X-RIL-Sync": "run"})
    assert response.status_code == 200
    assert response.json() == {
        "sent": 2,
        "received": 3,
        "bodies_sent": 1,
        "bodies_received": 4,
    }
    assert seen["url"] == "https://example.com/ril"


def test_running_a_sync_without_a_remote_is_a_conflict(client):
    response = client.post("/api/sync/run", headers={"X-RIL-Sync": "run"})
    assert response.status_code == 409


def test_an_unreachable_remote_is_not_the_page_failing(client, monkeypatch):
    _configure_sync(monkeypatch)

    def _down(data_folder, remote):
        raise RemoteUnavailableError("Could not reach the server.")

    monkeypatch.setattr(server_module, "_sync_with_remote", _down)
    response = client.post("/api/sync/run", headers={"X-RIL-Sync": "run"})
    assert response.status_code == 502
    assert "Could not reach" in response.json()["detail"]
