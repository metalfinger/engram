"""One protocol, two surfaces — threads carry the same floor as rooms.

Threads keep their transcript in GIT (permanent, versioned, the record behind the
Office conference rooms and the meetings widget). Everything else that made rooms
work — whose turn it is, who is listening, who has gone, escalating to the person
— is protocol rather than storage, so it applies to both. The coordination state
lives in a hidden shadow room: no extra git writes, no duplicated protocol.
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
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    return registry


def _login(monkeypatch, email="alice@example.com"):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


def _as_session(monkeypatch, key):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: key)


@pytest.mark.asyncio
async def test_a_thread_post_returns_a_floor(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post("handoff", "windows", "starting")
    assert "floor" in out, "threads carry the same floor as rooms"
    assert out["floor"]["alone"] is True, "nobody else has joined yet"


@pytest.mark.asyncio
async def test_speaking_in_a_thread_hands_the_floor_on(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handoff", "windows", "over to you")
    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_thread_read("handoff", sender="mac")
    assert read["floor"]["is_you"] is True
    assert "Your turn" in read["floor"]["do_next"]

    out = await app_module.kb_thread_post("handoff", "mac", "taking it")
    assert out["floor"]["is_you"] is False
    _as_session(monkeypatch, "sess-a")
    back = await app_module.kb_thread_read("handoff", sender="windows")
    assert back["floor"]["is_you"] is True


@pytest.mark.asyncio
async def test_reading_a_thread_registers_you(mu, monkeypatch):
    """Without this the first speaker addresses an empty house, exactly as rooms
    did before the same fix."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handoff", "windows", "anyone?")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("handoff", sender="mac")
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post("handoff", "windows", "still here")
    assert out["floor"]["alone"] is False
    assert {s["name"] for s in out["floor"]["speakers"]} == {"windows", "mac"}


@pytest.mark.asyncio
async def test_a_thread_can_block_on_the_human(mu, monkeypatch):
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append((kind, body)),
    )
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post(
        "handoff", "windows", "stuck", ask_human="Ship it or hold?"
    )
    assert out["floor"]["holder_name"] == "the human"
    assert out["floor"]["awaiting_human"] == "Ship it or hold?"
    assert any(k == "room_question" for k, _ in sent), "he has to be told"


@pytest.mark.asyncio
async def test_the_transcript_stays_in_git_not_in_the_shadow_room(mu, monkeypatch):
    """The shadow room holds floor state ONLY. Writing turns into it would put
    phantom messages in a transcript and burn a budget nobody reads."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handoff", "windows", "a real message")
    fid = app_module._thread_floor_id("handoff")  # noqa: SLF001
    turns = mu.rooms.read_turns(fid, mu.tenancy.user_by_handle("alice").id)
    assert [t for t in turns if t.kind == "message"] == []


@pytest.mark.asyncio
async def test_shadow_rooms_are_invisible_in_kb_rooms(mu, monkeypatch):
    """Otherwise every thread grows a duplicate entry beside it."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handoff", "windows", "hello")
    await app_module.kb_room_open("real-room", "an actual room")
    listing = await app_module.kb_rooms()
    assert [r["name"] for r in listing["rooms"]] == ["real-room"]


@pytest.mark.asyncio
async def test_a_thread_floor_survives_a_reconnect(mu, monkeypatch):
    """Same fix as rooms: the name is the identity, the session key is not."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handoff", "windows", "before")
    _as_session(monkeypatch, "sess-a-reborn")
    out = await app_module.kb_thread_read("handoff", sender="windows")
    names = [s["name"] for s in out["floor"]["speakers"]]
    assert names.count("windows") == 1, "one session, one row, across a reconnect"
