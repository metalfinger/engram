"""/dashboard/api/status — the zero-token feed behind the status line and the
prompt-checkpoint hook.

This endpoint had NO tests, which is exactly how `your_turn` shipped
structurally empty: it asked floor_state whether the Claude Code SESSION id held
the floor, but floors are held by MCP speaker keys — a different namespace, so
is_you was false forever, and an empty-because-broken list looks identical to an
empty-because-quiet one. Machine-level semantics now: "a turn is waiting on your
side and nobody is parked to take it."
"""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import engram_server.app as app_module
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    s = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
        "dashboard_session_secret": "z" * 40,
    })
    registry = StoreRegistry(s)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", s)
    monkeypatch.setattr(app_module, "_presence_last", {})
    monkeypatch.setattr(app_module._dashboard, "registry", registry)
    inv = registry.tenancy.create_invite("alice@example.com")
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "github", "github:alice")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="github:alice"))
    return registry


def _tok():
    return app_module._dashboard.auth.issue(
        "github:alice", "alice@example.com", "alice", ttl=3600, scope="notify")


@pytest.fixture()
def client():
    return TestClient(app_module.mcp.streamable_http_app())


def _status(client):
    r = client.get("/dashboard/api/status",
                   headers={"Authorization": f"Bearer {_tok()}"})
    assert r.status_code == 200
    return r.json()


@pytest.mark.asyncio
async def test_a_waiting_turn_shows_up_without_the_shadow_prefix(mu, client, monkeypatch):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("status-check", "windows", "over to you")
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-b")
    # mac reads and claims the open floor, then goes off to work (not listening):
    # a turn is now waiting on this machine with nobody parked to take it.
    await app_module.kb_thread_read("status-check", sender="mac")

    out = _status(client)
    assert "status-check" in out["your_turn"], (
        "a held, un-listened floor is exactly what the prompt checkpoint exists "
        "to surface"
    )
    assert not any(t.startswith("thread--") for t in out["your_turn"])


@pytest.mark.asyncio
async def test_a_parked_holder_is_not_nagged_about(mu, client, monkeypatch):
    """A holder inside a long-poll will wake itself — the bus covers rung one of
    the ladder, so the checkpoint saying it too would just be noise."""
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("parked-holder", "windows", "over to you")
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-b")
    await app_module.kb_thread_read("parked-holder", sender="mac")

    fid = app_module._thread_floor_id("parked-holder")
    me = app_module._require_room_user()
    app_module.registry.rooms.touch_speaker(fid, "sess-b", me.id, name="mac",
                                            listening_seconds=60)
    out = _status(client)
    assert "parked-holder" not in out["your_turn"]


def test_no_token_no_status(mu, client):
    assert client.get("/dashboard/api/status").status_code == 401
