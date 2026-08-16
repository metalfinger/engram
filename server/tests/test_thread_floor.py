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
from engram_server.errors import KBError
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
async def test_the_floor_moves_before_the_transcript_wakes_anyone(mu, monkeypatch):
    """Found live: mac-a posted and a session parked on the thread read the turn
    with the floor UNMOVED and last_spoke still empty. The git write is what wakes
    waiters, so every bit of coordination state has to settle before it — rooms
    never had this because post_turn moves the floor in the same transaction."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("race", "windows", "opening")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("race", sender="mac")
    out = await app_module.kb_thread_post("race", "mac", "my turn done")
    # By the time the post returns, the floor must already have moved on.
    assert out["floor"]["is_you"] is False
    speakers = {s["name"]: s for s in out["floor"]["speakers"]}
    assert speakers["mac"]["last_spoke"], "speaking must be recorded, not skipped"


@pytest.mark.asyncio
async def test_threads_report_server_ms(mu, monkeypatch):
    """Without it, a session timing a thread park is really timing itself."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    posted = await app_module.kb_thread_post("timed", "windows", "hello")
    assert isinstance(posted["server_ms"], int)
    read = await app_module.kb_thread_read("timed", sender="windows")
    assert isinstance(read["server_ms"], int)


@pytest.mark.asyncio
async def test_a_stale_thread_cursor_returns_what_you_missed(mu, monkeypatch):
    """Same protection as rooms — with the thread's own cursor type (ISO string),
    since the cursors differ by surface and pretending otherwise would break both."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    first = await app_module.kb_thread_read("stale", sender="windows")
    await app_module.kb_thread_post("stale", "windows", "opening")
    cursor = (await app_module.kb_thread_read("stale", sender="windows"))["cursor"]

    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_post("stale", "mac", "you missed this")

    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post(
        "stale", "windows", "composed from a stale view", expect_cursor=cursor
    )
    assert [t["message"] for t in out["missed"]] == ["you missed this"]
    assert any("while you were composing" in w for w in out["warnings"])
    assert first is not None


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


@pytest.mark.asyncio
async def test_a_long_thread_is_told_to_end(mu, monkeypatch):
    """Rooms carry a turn budget so agent conversations terminate instead of
    agreeing politely forever. Threads had only a per-minute rate limit, which
    stops a burst and not a runaway — an asymmetry that only mattered once
    threads became first-class for agent-to-agent work."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_THREAD_TURN_BUDGET", 2)
    monkeypatch.setattr(app_module, "_THREAD_TURN_CAP", 4)
    for i in range(3):
        out = await app_module.kb_thread_post("long", "windows", f"turn {i}")
    assert any("close the thread" in w for w in out.get("warnings", []))


@pytest.mark.asyncio
async def test_a_very_long_thread_is_told_to_stop(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_THREAD_TURN_BUDGET", 2)
    monkeypatch.setattr(app_module, "_THREAD_TURN_CAP", 4)
    for i in range(5):
        out = await app_module.kb_thread_post("longer", "windows", f"turn {i}")
    assert any("past the" in w for w in out.get("warnings", []))


@pytest.mark.asyncio
async def test_a_thread_block_can_be_answered_by_relay(mu, monkeypatch):
    """The gap that mattered most: ask_human on a THREAD blocked it and notified
    Hiren, but kb_room_relay_answer looked for a room by that name and a thread
    has none — so the thread could stay blocked forever. Threads keep their turns
    in git, so nothing passes through the room tables to notice the answer."""
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post(
        "blocked-thread", "windows", "stuck", ask_human="Ship it or hold?"
    )
    assert out["floor"]["awaiting_human"] == "Ship it or hold?"

    relayed = await app_module.kb_room_relay_answer("blocked-thread", "Hold it.")
    assert relayed["turn"]["via"] == "human"
    assert relayed["turn"]["relayed"] is True
    assert relayed["floor"]["awaiting_human"] == ""

    read = await app_module.kb_thread_read("blocked-thread", sender="windows")
    assert read["floor"]["is_you"] is True, "floor returns to whoever asked"
    assert any("Hold it." in t["message"] for t in read["turns"])


@pytest.mark.asyncio
async def test_relaying_to_nothing_is_refused(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    with pytest.raises(KBError, match="No room or thread"):
        await app_module.kb_room_relay_answer("does-not-exist-anywhere", "hi")


@pytest.mark.asyncio
async def test_a_thread_turn_for_a_gone_session_reaches_the_tray(mu, monkeypatch):
    """The delivered verdict existed on rooms only — so the surface a single user
    with several machines actually uses was the one that stayed silent when a
    handoff landed for a session that had gone home."""
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append((kind, body)),
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handover", "windows", "opening")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("handover", sender="mac")
    fid = app_module._thread_floor_id("handover")  # noqa: SLF001
    with mu.rooms._lock:  # noqa: SLF001 — mac goes home
        mu.rooms._conn.execute(
            "UPDATE room_speakers SET last_seen = '2000-01-01T00:00:00Z' "
            "WHERE room_id = ? AND name = 'mac'", (fid,),
        )
        mu.rooms._conn.commit()
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handover", "windows", "here's the handoff")
    assert [b for k, b in sent if k == "room_turn"], "nothing else could deliver it"


@pytest.mark.asyncio
async def test_a_live_thread_session_is_not_notified(mu, monkeypatch):
    """A quiet channel is the feature — an active exchange must not ping the tray."""
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append(kind),
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("chatty", "windows", "opening")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("chatty", sender="mac")
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("chatty", "windows", "still going")
    assert "room_turn" not in sent


@pytest.mark.asyncio
async def test_closing_a_thread_offers_a_precipitate(mu, monkeypatch):
    """A transcript is not knowledge — the next session should not have to re-read
    the whole exchange to learn what was decided."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("wrap", "windows", "opening")
    out = await app_module.kb_thread_post("wrap", "windows", "done", close=True)
    assert "precipitate_instruction" in out
    assert "only on their explicit yes" in out["precipitate_instruction"]
