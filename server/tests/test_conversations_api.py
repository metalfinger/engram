"""GET /dashboard/api/conversations — helix's live rooms/threads panel feed.

The client side shipped FIRST and sat dark waiting for this endpoint
(spec: projects/helix-control-panel/specs/rooms-endpoint-spec.md). Contract:
open + last-24h only, holder from the floor, blocked-on-human flags, last 1-3
turns oldest-first truncated ~200, every field optional client-side.
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


def _get(client):
    r = client.get("/dashboard/api/conversations",
                   headers={"Authorization": f"Bearer {_tok()}"})
    assert r.status_code == 200
    return r.json()


def test_needs_a_token(mu, client):
    assert client.get("/dashboard/api/conversations").status_code == 401


@pytest.mark.asyncio
async def test_an_active_thread_appears_with_holder_and_last_turns(mu, client, monkeypatch):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("panel-test", "windows", "first turn")
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-b")
    await app_module.kb_thread_read("panel-test", sender="mac")
    await app_module.kb_thread_post("panel-test", "mac",
                                    "second turn " + "x" * 400)

    out = _get(client)
    t = next(x for x in out["threads"] if x["name"] == "panel-test")
    assert t["holder"] == "windows", "floor passed back after mac spoke"
    assert [x["speaker"] for x in t["last"]] == ["windows", "mac"], "oldest first"
    assert len(t["last"][-1]["text"]) <= 201, "server-side truncation (~200 + ellipsis)"
    assert {p["speaker"] for p in t["participants"]} == {"windows", "mac"}
    assert "path" in t["participants"][0], "path key present even when empty"


@pytest.mark.asyncio
async def test_shadow_rooms_never_leak_as_rooms(mu, client, monkeypatch):
    """A thread must appear ONCE, in threads — its shadow room is plumbing, and
    leaking it as a room would put a duplicate beside every thread."""
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("no-leak", "windows", "hello")
    out = _get(client)
    assert all(not r["name"].startswith("thread--") for r in out["rooms"])
    assert not any(r["name"] == "no-leak" for r in out["rooms"])


@pytest.mark.asyncio
async def test_a_closed_thread_is_noise_and_omitted(mu, client, monkeypatch):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("done-thread", "windows", "over")
    await app_module.kb_thread_post("done-thread", "windows", "bye", close=True)
    out = _get(client)
    assert not any(t["name"] == "done-thread" for t in out["threads"])
