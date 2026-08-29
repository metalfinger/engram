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
    assert any(e.startswith("status-check") for e in out["your_turn"]), (
        "a held, un-listened floor is exactly what the prompt checkpoint exists "
        "to surface"
    )
    assert not any(t.startswith("thread--") for t in out["your_turn"])
    # THE DEBTOR IS NAMED. Both machines showed an identical ⏳ for a floor only
    # one of them held; carrying the holder's name lets each machine read
    # mine-or-not without the impossible session join.
    assert any(e == "status-check → mac" for e in out["your_turn"]), out["your_turn"]


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


@pytest.mark.asyncio
async def test_a_closed_thread_stops_claiming_turns(mu, client, monkeypatch):
    """Found by the prompt-checkpoint hook on its FIRST live firing: it named a
    thread closed the night before. The shadow room outlived its thread, so the
    last floor-holder read as a waiting turn on every prompt, forever. The room
    dies with the thread now."""
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("ghost-turn", "windows", "over to you")
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-b")
    await app_module.kb_thread_read("ghost-turn", sender="mac")
    assert any(e.startswith("ghost-turn") for e in _status(client)["your_turn"]),         "live thread: listed"

    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("ghost-turn", "windows", "done here", close=True)
    out = _status(client)
    assert not any(e.startswith("ghost-turn") for e in out["your_turn"]), (
        "a closed thread must not nag on every prompt for the rest of time"
    )


@pytest.mark.asyncio
async def test_a_long_gone_holder_is_abandoned_not_your_turn(mu, client, monkeypatch):
    """Field case: two vibechk rooms abandoned on 18 Aug read as "⏳ your turn"
    until 24 Aug. Six days of a false imperative teaches the reader to ignore
    the one segment that must never become ignorable. Same fact, honest bucket."""
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    await app_module.kb_thread_post("gone-holder", "windows", "over to you")
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-b")
    await app_module.kb_thread_read("gone-holder", sender="mac")  # mac now holds

    out = _status(client)
    assert any(e.startswith("gone-holder") for e in out["your_turn"]),         "a LIVE holder is actionable now"
    assert not any(e.startswith("gone-holder") for e in out["abandoned"])

    # The holder's last_seen ages past the liveness window: same room, new bucket.
    fid = app_module._thread_floor_id("gone-holder")
    app_module.registry.rooms._conn.execute(
        "UPDATE room_speakers SET last_seen = '2026-08-01T00:00:00Z', "
        "listening_until = '' WHERE room_id = ? AND speaker = 'sess-b'", (fid,))
    app_module.registry.rooms._conn.commit()

    out = _status(client)
    assert any(e.startswith("gone-holder") for e in out["abandoned"])
    assert not any(e.startswith("gone-holder") for e in out["your_turn"])


@pytest.mark.asyncio
async def test_status_serves_the_hooks_digest_for_drift_detection(mu, client):
    """Hiren's presence hook sat a version behind for a day: the attention-upload
    fix was deployed and simply never ran on his machine, and nothing said so.
    kb_setup_machine can detect drift but only when someone thinks to run it —
    serving the digest lets the status line notice by itself."""
    import engram_server.app as app_module_
    out = _status(client)
    assert out["hooks_digest"], "the status feed must carry the current digest"
    assert out["hooks_digest"] == app_module_._dir_digest(
        app_module_._HOOKS_DIR, app_module_._owned_hooks(app_module_._HOOKS_DIR)
    ), "must match the recipe a machine compares against"
