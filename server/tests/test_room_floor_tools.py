"""Floor control at the TOOL layer — the wiring the store tests cannot reach.

`_speaker_key()` reads the live MCP session, so in tests it returns '' and floor
control would silently never engage. These tests pin it to fixed keys to simulate
two sessions of ONE user talking to each other: the configuration that broke, and
the one Hiren actually runs.
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
    """Pretend this tool call arrives on MCP session `key`."""
    monkeypatch.setattr(app_module, "_speaker_key", lambda: key)


@pytest.mark.asyncio
async def test_two_sessions_of_one_user_take_turns(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("pairing", "two sessions, one user")
    await app_module.kb_room_post("pairing", "starting on the parser", speaker="windows")

    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_room_read("pairing")
    assert read["floor"]["is_you"] is True, "session B was handed the floor"

    out = await app_module.kb_room_post("pairing", "I'll take the tests", speaker="mac")
    assert out["floor"]["is_you"] is False
    _as_session(monkeypatch, "sess-a")
    assert (await app_module.kb_room_read("pairing"))["floor"]["is_you"] is True


@pytest.mark.asyncio
async def test_a_reply_from_the_same_users_other_session_is_seen(mu, monkeypatch):
    """The bug this whole wave rests on: replies were filtered by user_id, so two
    sessions of one person could never see each other and every wait timed out."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("pairing", "same user, two sessions")
    first = await app_module.kb_room_post("pairing", "question?", speaker="windows")

    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_post("pairing", "answer.", speaker="mac")

    _as_session(monkeypatch, "sess-a")
    fresh = mu.rooms.read_turns(
        mu.rooms.room_by_name("pairing").id,
        mu.tenancy.user_by_handle("alice").id,
        since_id=first["turn"]["id"],
    )
    replies = [t for t in fresh if t.kind != "guest_read"]
    assert replies, "the other session's turn must count as a reply"
    assert replies[0].body == "answer."


@pytest.mark.asyncio
async def test_the_transcript_says_which_session_spoke(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("pairing", "attribution")
    await app_module.kb_room_post("pairing", "from windows", speaker="windows")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_post("pairing", "from mac", speaker="mac")

    turns = (await app_module.kb_room_read("pairing"))["turns"]
    msgs = [t for t in turns if t["kind"] == "message"]
    assert [t["speaker"] for t in msgs] == ["sess-a", "sess-b"], (
        "one handle, two voices — without this the transcript reads as one session "
        "talking to itself"
    )
    assert all(t["via"] == "claude" for t in msgs)


@pytest.mark.asyncio
async def test_a_lone_session_is_warned_instead_of_waiting(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("solo", "nobody else here")
    out = await app_module.kb_room_post(
        "solo", "anyone about?", speaker="windows", wait_for_reply=True, wait_seconds=120
    )
    assert out["floor"]["alone"] is True
    assert any("no one to reply" in w for w in out["warnings"])
    assert "waited" not in out, "it must not burn 120s waiting for nobody"


@pytest.mark.asyncio
async def test_asking_the_human_blocks_the_room_and_notifies(mu, monkeypatch):
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append((kind, body, ref)),
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("decision", "needs a person")
    out = await app_module.kb_room_post(
        "decision", "blocked on a call I can't make", speaker="windows",
        ask_human="Ship behind a flag, or hold?",
    )
    assert out["floor"]["holder_name"] == "the human"
    assert sent and sent[0][0] == "room_question"
    assert "Ship behind a flag" in sent[0][1]

    # another session arriving sees WHY, and is told not to wait on an agent
    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_room_read("decision")
    assert read["floor"]["awaiting_human"] == "Ship behind a flag, or hold?"
    assert read["floor"]["is_you"] is False
    second = await app_module.kb_room_post(
        "decision", "noted", speaker="mac", wait_for_reply=True, wait_seconds=120
    )
    assert any("blocked on the person" in w for w in second["warnings"])
    assert "waited" not in second


@pytest.mark.asyncio
async def test_kb_rooms_flags_which_rooms_are_waiting_on_you(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("alpha", "first")
    await app_module.kb_room_open("beta", "second")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_read("beta")  # register B as present in beta only
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_post("beta", "your move", speaker="windows")

    _as_session(monkeypatch, "sess-b")
    listing = await app_module.kb_rooms()
    assert listing["waiting_on_you"] == ["beta"], (
        "a session must be able to see, in one cheap call, which rooms owe a reply"
    )


@pytest.mark.asyncio
async def test_kb_rooms_returns_immediately_when_there_is_unread(mu, monkeypatch):
    """Parking on top of unread turns is how a session misses the very message it
    was called to read."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("alpha", "first", invite="bob")
    # Bob speaks, so alice genuinely has something unread waiting for her.
    _login(monkeypatch, "bob@example.com")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_post("alpha", "something new", speaker="bob-mac")

    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    listing = await app_module.kb_rooms(wait_seconds=120)
    assert "woke_on" not in listing, "unread present — it must not have parked at all"
    assert listing["rooms"][0]["unread"] >= 1


@pytest.mark.asyncio
async def test_a_turn_nobody_is_parked_on_reaches_the_human(mu, monkeypatch):
    """Parked → instant off the bus. Rested → unreachable by any wait, so the only
    honest move is to notify the person, whose tray is still live."""
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append((kind, body)),
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("pairing", "one side has gone home")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_read("pairing")  # B joins, then stops listening
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_post("pairing", "are you there?", speaker="windows")
    assert any(k == "room_turn" for k, _ in sent), (
        "nobody was parked, so nothing would have delivered this turn"
    )


@pytest.mark.asyncio
async def test_one_person_gets_one_notification_however_many_sessions(mu, monkeypatch):
    """Several sessions of one user are one human with one tray. Notifying per
    speaker pinged Hiren twice for a single turn in a three-session room, and
    would scale with however many sessions he had open — the sort of noise that
    trains someone to ignore notifications entirely."""
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append(uid),
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("crowded", "three sessions, one person")
    for s, n in (("sess-b", "mac-a"), ("sess-c", "mac-b")):
        _as_session(monkeypatch, s)
        await app_module.kb_room_read("crowded", speaker=n)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_post("crowded", "nobody is parked", speaker="windows")
    assert len(sent) == 1, f"one tray, one ping — got {len(sent)}"


@pytest.mark.asyncio
async def test_a_listening_session_is_not_notified(mu, monkeypatch):
    """A parked session already gets the turn instantly; notifying as well would
    make every busy room spam the user's tray."""
    _login(monkeypatch)
    sent = []
    monkeypatch.setattr(
        app_module, "_push_notification",
        lambda uid, kind, body, ref=None: sent.append((kind, body)),
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("pairing", "the other side is parked")
    _as_session(monkeypatch, "sess-b")
    mu.rooms.touch_speaker(
        mu.rooms.room_by_name("pairing").id, "sess-b",
        mu.tenancy.user_by_handle("alice").id, name="mac", listening_seconds=60,
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_post("pairing", "here you go", speaker="windows")
    assert not any(k == "room_turn" for k, _ in sent)


@pytest.mark.asyncio
async def test_posting_against_a_stale_cursor_returns_what_you_missed(mu, monkeypatch):
    """The failure every session hit in the first live test: read, think for 30
    seconds, then assert something the room had already moved past — one of them
    reported a member absent who had been present for 24 minutes."""
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("busy", "the room moves while you write")
    first = await app_module.kb_room_post("busy", "opening", speaker="windows")
    cursor = first["turn"]["id"]

    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_post("busy", "something you missed", speaker="mac-a")

    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_room_post(
        "busy", "composed from a stale view", speaker="windows", expect_cursor=cursor
    )
    assert out["turn"]["id"], "the post still goes through — never block the intent"
    assert [t["body"] for t in out["missed"]] == ["something you missed"]
    assert any("while you were composing" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_a_current_cursor_reports_nothing_missed(mu, monkeypatch):
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("calm", "nothing moved")
    first = await app_module.kb_room_post("calm", "opening", speaker="windows")
    out = await app_module.kb_room_post(
        "calm", "still current", speaker="windows", expect_cursor=first["turn"]["id"]
    )
    assert "missed" not in out


@pytest.mark.asyncio
async def test_an_unnamed_session_is_warned(mu, monkeypatch):
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("anon", "nobody named themselves")
    out = await app_module.kb_room_post("anon", "who am I")
    assert any("named yourself" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_do_next_says_what_to_do(mu, monkeypatch):
    """Five booleans is four too many — every session reasoned its way to the same
    action from the same flags."""
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("advice", "one sentence, not five flags")
    out = await app_module.kb_room_post("advice", "alone here", speaker="windows")
    assert "Don't wait" in out["floor"]["do_next"]

    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_room_read("advice", speaker="mac-a")
    assert "Your turn" in read["floor"]["do_next"]


@pytest.mark.asyncio
async def test_do_next_points_at_the_human_when_blocked(mu, monkeypatch):
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("blocked", "needs a person")
    out = await app_module.kb_room_post(
        "blocked", "stuck", speaker="windows", ask_human="Ship or hold?"
    )
    assert "kb_room_relay_answer" in out["floor"]["do_next"]
    assert "Ship or hold?" in out["floor"]["do_next"]


@pytest.mark.asyncio
async def test_a_relayed_answer_unblocks_the_room(mu, monkeypatch):
    """The user answers wherever they already are — in chat with their own Claude
    — instead of being made to open a web page they rarely visit."""
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("decide", "needs a person")
    await app_module.kb_room_post(
        "decide", "blocked", speaker="windows", ask_human="Flag or hold?"
    )
    _as_session(monkeypatch, "sess-b")
    out = await app_module.kb_room_relay_answer("decide", "Hold it for now.")
    assert out["turn"]["via"] == "human", "their decision, so it counts as theirs"
    assert out["turn"]["relayed"] is True, "but never claim they were in the room"
    assert out["floor"]["awaiting_human"] == ""

    _as_session(monkeypatch, "sess-a")
    read = await app_module.kb_room_read("decide", speaker="windows")
    assert read["floor"]["is_you"] is True, "the floor returns to whoever asked"


@pytest.mark.asyncio
async def test_relaying_nothing_is_refused(mu, monkeypatch):
    """The guard against inventing an answer nobody gave."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("decide", "needs a person")
    with pytest.raises(KBError, match="ask the user"):
        await app_module.kb_room_relay_answer("decide", "   ")


@pytest.mark.asyncio
async def test_kb_rooms_surfaces_rooms_blocked_on_the_user(mu, monkeypatch):
    """A blocked room is invisible to the person unless a session tells them."""
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("decide", "needs a person")
    await app_module.kb_room_post(
        "decide", "blocked", speaker="windows", ask_human="Ship it or hold?"
    )
    listing = await app_module.kb_rooms()
    assert listing["needs_the_user"] == {"decide": "Ship it or hold?"}


@pytest.mark.asyncio
async def test_closing_over_an_unanswered_question_says_so(mu, monkeypatch):
    """The question to the person is the one thing in the room that was waiting on
    them; closing must not swallow it silently."""
    _login(monkeypatch)
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("decide", "needs a person")
    await app_module.kb_room_post(
        "decide", "blocked", speaker="windows", ask_human="Flag or hold?"
    )
    closed = await app_module.kb_room_close("decide", outcome="wrapped up")
    assert any("Flag or hold?" in w for w in closed["warnings"])


@pytest.mark.asyncio
async def test_floor_degrades_safely_without_a_session_key(mu, monkeypatch):
    """The dashboard, the web composer and older clients have no MCP session. They
    must keep working exactly as before rather than erroring."""
    _login(monkeypatch)
    _as_session(monkeypatch, "")
    await app_module.kb_room_open("legacy", "no session key")
    out = await app_module.kb_room_post("legacy", "still fine", speaker="whoever")
    assert out["turn"]["id"]
    assert "speaker" not in out["turn"]
    assert out["floor"]["holder"] == ""
