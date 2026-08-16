"""Item 1 — a handoff surfaces when the conversation has stalled.

Earlier today a session was told "the holder has gone — take the floor" and had
no idea what that holder had been doing. A handoff record is exactly that missing
context; it was written, readable, and invisible from inside the conversation.
"""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    s = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
    })
    registry = StoreRegistry(s)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", s)
    monkeypatch.setattr(app_module, "_presence_last", {})
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})
    monkeypatch.setattr(app_module, "_HANDOFF_CACHE", {"at": 0.0, "rows": []})
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    return registry


def _login(monkeypatch, email="alice@example.com"):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


def _as_session(monkeypatch, key):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: key)


async def _stall(mu, monkeypatch, room_name):
    """Open a room with two speakers, then age the floor-holder out."""
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open(room_name, "someone will leave")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_read(room_name, speaker="mac-a")
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_post(room_name, "over to you", speaker="windows")
    rid = mu.rooms.room_by_name(room_name).id
    with mu.rooms._lock:  # noqa: SLF001
        mu.rooms._conn.execute(
            "UPDATE room_speakers SET last_seen = '2000-01-01T00:00:00Z' "
            "WHERE room_id = ? AND name = 'mac-a'", (rid,),
        )
        mu.rooms._conn.commit()


@pytest.mark.asyncio
async def test_a_stalled_room_surfaces_the_handoff(mu, monkeypatch):
    _login(monkeypatch)
    await app_module.kb_handoff(
        "mac-a", "Mid-way through the widget reskin; theme port done, tests green.",
        next_steps="Review the radius change before merging.",
    )
    monkeypatch.setattr(app_module, "_HANDOFF_CACHE", {"at": 0.0, "rows": []})
    await _stall(mu, monkeypatch, "stalled-room")
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_room_read("stalled-room", speaker="windows")
    assert out["floor"]["stalled"] is True
    assert out["floor"]["handoffs"][0]["from"] == "mac-a"
    assert "widget reskin" in out["floor"]["handoffs"][0]["summary"]
    assert "kb_read its path" in out["floor"]["do_next"]


@pytest.mark.asyncio
async def test_a_healthy_room_does_not_carry_handoffs(mu, monkeypatch):
    """Not wallpaper: it appears only when it changes what you would do."""
    _login(monkeypatch)
    await app_module.kb_handoff("mac-a", "Some earlier handover.")
    monkeypatch.setattr(app_module, "_HANDOFF_CACHE", {"at": 0.0, "rows": []})
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("healthy", "everyone is here")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_read("healthy", speaker="mac-a")
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_room_post("healthy", "hello", speaker="windows")
    assert out["floor"]["stalled"] is False
    assert "handoffs" not in out["floor"]


@pytest.mark.asyncio
async def test_a_stall_with_no_handoff_says_nothing_extra(mu, monkeypatch):
    _login(monkeypatch)
    await _stall(mu, monkeypatch, "quiet-stall")
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_room_read("quiet-stall", speaker="windows")
    assert out["floor"]["stalled"] is True
    assert "handoffs" not in out["floor"]
    assert "kb_read its path" not in out["floor"]["do_next"]


@pytest.mark.asyncio
async def test_threads_get_it_too(mu, monkeypatch):
    """One protocol, two surfaces."""
    _login(monkeypatch)
    await app_module.kb_handoff("mac-b", "Parked the reskin pending design.")
    monkeypatch.setattr(app_module, "_HANDOFF_CACHE", {"at": 0.0, "rows": []})
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handover-thread", "windows", "opening")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("handover-thread", sender="mac-a")
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handover-thread", "windows", "over to you")
    fid = app_module._thread_floor_id("handover-thread")  # noqa: SLF001
    with mu.rooms._lock:  # noqa: SLF001
        mu.rooms._conn.execute(
            "UPDATE room_speakers SET last_seen = '2000-01-01T00:00:00Z' "
            "WHERE room_id = ? AND name = 'mac-a'", (fid,),
        )
        mu.rooms._conn.commit()
    out = await app_module.kb_thread_read("handover-thread", sender="windows")
    assert out["floor"]["stalled"] is True
    assert out["floor"]["handoffs"][0]["from"] == "mac-b"


@pytest.mark.asyncio
async def test_handoffs_failing_never_breaks_a_turn(mu, monkeypatch):
    _login(monkeypatch)
    await _stall(mu, monkeypatch, "resilient-stall")
    monkeypatch.setattr(app_module, "_HANDOFF_CACHE", {"at": 0.0, "rows": []})

    async def _boom(limit=3):
        raise RuntimeError("handoff store on fire")

    monkeypatch.setattr((await app_module.current_store()), "kb_recent_handoffs", _boom)
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_room_read("resilient-stall", speaker="windows")
    assert out["floor"]["stalled"] is True
    assert "handoffs" not in out["floor"]
