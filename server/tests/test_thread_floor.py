"""One protocol, two surfaces — threads carry the same floor as rooms.

Threads keep their transcript in GIT (permanent, versioned, the record behind the
Office conference rooms and the meetings widget). Everything else that made rooms
work — whose turn it is, who is listening, who has gone, escalating to the person
— is protocol rather than storage, so it applies to both. The coordination state
lives in a hidden shadow room: no extra git writes, no duplicated protocol.
"""

import time
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


@pytest.mark.asyncio
async def test_a_turn_is_delivered_but_unread_until_someone_reads_it(mu, monkeypatch):
    """The state that had no name. A session doing real work is deaf until it
    chooses to look — which used to be indistinguishable from having left, because
    presence flags say somebody is THERE, never what they have CONSUMED."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("delivery", "windows", "first")
    # b joins and reads, so both are known speakers.
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("delivery", sender="mac")
    await app_module.kb_thread_post("delivery", "mac", "second")

    # a speaks again without b having read it.
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post("delivery", "windows", "third")
    floor = out["floor"]
    assert floor["latest_seq"] >= 2
    mac = next(s for s in floor["others"] if s["name"] == "mac")
    assert mac["unread"] >= 1, "mac has not read the newest turn"
    assert floor["unread_by_others"].get("mac") == mac["unread"]

    # Now mac reads, and the unread collapses — for mac specifically.
    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_thread_read("delivery", sender="mac")
    me = next(s for s in read["floor"]["speakers"] if s["name"] == "mac")
    assert me["unread"] == 0
    assert me["read_through"] == read["floor"]["latest_seq"]


@pytest.mark.asyncio
async def test_your_own_turn_is_never_unread_to_you(mu, monkeypatch):
    """Otherwise a speaker shows as having unread mail consisting entirely of
    their own message — which is how a phantom backlog gets reported."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_thread_post("selfread", "windows", "hello")
    me = next(s for s in out["floor"]["speakers"] if s["name"] == "windows")
    assert me["unread"] == 0


@pytest.mark.asyncio
async def test_read_through_never_walks_backwards(mu, monkeypatch):
    """A read is a high-water mark. A stale or out-of-order call must not
    resurrect turns someone has already answered — which, on a reconnect, is
    exactly the call that arrives late."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("watermark", "windows", "one")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("watermark", sender="mac")
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("watermark", "windows", "two")
    _as_session(monkeypatch, "sess-b")
    caught_up = await app_module.kb_thread_read("watermark", sender="mac")
    high = next(s for s in caught_up["floor"]["speakers"]
                if s["name"] == "mac")["read_through"]

    fid = app_module._thread_floor_id("watermark")
    app_module.registry.rooms.mark_read(fid, "sess-b", 1)  # a late, stale stamp
    after = app_module.registry.rooms.floor_state(fid, "sess-b")
    still = next(s for s in after["speakers"] if s["speaker"] == "sess-b")
    assert still["read_through"] == high


@pytest.mark.asyncio
async def test_a_turn_landing_between_polls_is_not_lost(mu, monkeypatch):
    """The reconnect race, asserted rather than reasoned about. A long-poll caps
    below the host's kill, so there is always a gap between one call returning and
    the next opening. Turns live in git and are filtered by cursor — they are not
    delivered-and-discarded — so the next read with the same cursor still gets it."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-b")
    empty = await app_module.kb_thread_read("gap", sender="mac", wait_seconds=0)
    cursor = empty["cursor"]

    # A turn lands while nobody is inside a poll at all.
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("gap", "windows", "landed in the gap")

    _as_session(monkeypatch, "sess-b")
    late = await app_module.kb_thread_read("gap", sender="mac", since=cursor)
    assert [t["message"] for t in late["turns"]] == ["landed in the gap"]


@pytest.mark.asyncio
async def test_a_waiting_read_wakes_the_instant_a_turn_lands(mu, monkeypatch):
    """Threads used to sleep-poll at 1s while rooms woke on a condition variable —
    the difference between 'fast' and 'realtime', on the surface a single user with
    several machines actually uses. Same bus now.

    Asserted as latency, because a wake that works but arrives on the old 1s tick
    would pass any correctness-only test."""
    import anyio

    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    first = await app_module.kb_thread_post("wake", "windows", "opening")
    cursor = first["posted"]

    async def _speak_soon():
        await anyio.sleep(0.2)
        _as_session(monkeypatch, "sess-a")
        await app_module.kb_thread_post("wake", "windows", "here it is")

    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(_speak_soon)
        _as_session(monkeypatch, "sess-b")
        out = await app_module.kb_thread_read(
            "wake", sender="mac", since=cursor, wait_seconds=25,
        )
    elapsed = time.monotonic() - started

    assert [t["message"] for t in out["turns"]] == ["here it is"]
    assert elapsed < 1.0, f"woke on a clock, not the bus ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_a_wake_that_is_not_for_you_keeps_waiting(mu, monkeypatch):
    """A notify is not proof of a turn for THIS cursor — our own post wakes the bus
    too. Returning empty on any wake would turn a 25s wait into an instant miss:
    faster, and wrong."""
    import anyio

    _login(monkeypatch)
    _as_session(monkeypatch, "sess-b")
    seed = await app_module.kb_thread_post("selfwake", "mac", "mine")
    cursor = seed["posted"]

    started = time.monotonic()
    _as_session(monkeypatch, "sess-b")
    out = await app_module.kb_thread_read(
        "selfwake", sender="mac", since=cursor, wait_seconds=3,
    )
    elapsed = time.monotonic() - started
    assert out["turns"] == []
    assert elapsed >= 2.0, "returned early on a wake that carried nothing new"


@pytest.mark.asyncio
async def test_a_listener_that_fetched_is_not_the_same_as_an_agent_that_read(
    mu, monkeypatch,
):
    """pi-exec found this in my design, not theirs. Every reader the floor was built
    against was a model reading for itself, where fetch and consume are one event.
    Put a background listener in between — it holds the poll outside the model's turn
    and queues the turn until current work ends — and `read_through` says "read"
    during exactly the window Hiren wanted made visible."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("acked", "windows", "one")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("acked", sender="mac")

    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("acked", "windows", "two")

    # mac's LISTENER fetches, but its agent is mid-work and hasn't seen it.
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("acked", sender="mac")

    _as_session(monkeypatch, "sess-a")
    view = await app_module.kb_thread_post("acked", "windows", "three")
    mac = next(s for s in view["floor"]["others"] if s["name"] == "mac")
    assert mac["state"] == "unread", "turn three is with nobody yet"

    # The listener fetches three, then acks only through two.
    _as_session(monkeypatch, "sess-b")
    got = await app_module.kb_thread_read("acked", sender="mac")
    seq_three = max(t["seq"] for t in got["turns"])
    await app_module.kb_thread_read("acked", sender="mac", ack_through=seq_three - 1)

    _as_session(monkeypatch, "sess-a")
    after = await app_module.kb_thread_read("acked", sender="windows")
    mac = next(s for s in after["floor"]["others"] if s["name"] == "mac")
    assert mac["state"] == "delivered", "the listener has it; the agent does not"
    assert mac["unread"] == 0 and mac["unprocessed"] >= 1

    # Agent surfaces it.
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("acked", sender="mac", ack_through=seq_three)
    _as_session(monkeypatch, "sess-a")
    done = await app_module.kb_thread_read("acked", sender="windows")
    mac = next(s for s in done["floor"]["others"] if s["name"] == "mac")
    assert mac["state"] == "read"


@pytest.mark.asyncio
async def test_a_client_that_never_acks_stays_correct(mu, monkeypatch):
    """Claude Code fetches and consumes in one event, so it must need no change and
    must never look permanently 'delivered' for want of a stamp it has no reason to
    send. processed_through falls back to read_through."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    # THREE turns, deliberately. The first version of this test used one, whose seq
    # is 0 — and read_through was 0 too, so a broken default of 0 gave the same
    # answer as a correct fallback. It passed while the feature was broken in
    # production, which is the exact failure it was written to prevent.
    await app_module.kb_thread_post("noack", "windows", "one")
    await app_module.kb_thread_post("noack", "windows", "two")
    await app_module.kb_thread_post("noack", "windows", "three")
    _as_session(monkeypatch, "sess-b")
    out = await app_module.kb_thread_read("noack", sender="mac")
    me = next(s for s in out["floor"]["speakers"] if s["name"] == "mac")
    assert out["floor"]["latest_seq"] >= 2, "need turns past seq 0 to mean anything"
    assert me["state"] == "read"
    assert me["processed_through"] == me["read_through"]
    assert me["unprocessed"] == 0

    # And it stays true across further reads — the bug stamped on EVERY call.
    await app_module.kb_thread_read("noack", sender="mac")
    again = await app_module.kb_thread_read("noack", sender="mac")
    me = next(s for s in again["floor"]["speakers"] if s["name"] == "mac")
    assert me["state"] == "read"


@pytest.mark.asyncio
async def test_the_processed_watermark_never_walks_backwards(mu, monkeypatch):
    """Needs at least two turns to mean anything: with a single 0-seq turn there is
    no lower value to send, and the first version of this test 'failed' by acking
    ABOVE the top — which is a legitimate advance, not a regression."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("ackmono", "windows", "one")
    await app_module.kb_thread_post("ackmono", "windows", "two")
    _as_session(monkeypatch, "sess-b")
    got = await app_module.kb_thread_read("ackmono", sender="mac")
    top = max(t["seq"] for t in got["turns"])
    assert top >= 1
    await app_module.kb_thread_read("ackmono", sender="mac", ack_through=top)
    await app_module.kb_thread_read("ackmono", sender="mac", ack_through=0)  # stale
    out = await app_module.kb_thread_read("ackmono", sender="mac")
    me = next(s for s in out["floor"]["speakers"] if s["name"] == "mac")
    assert me["processed_through"] == top


@pytest.mark.asyncio
async def test_an_ack_beyond_the_last_turn_is_clamped(mu, monkeypatch):
    """Monotonic and unbounded is a bad pair: a client acking a seq that has not
    arrived would mark itself caught up on turns nobody has written, permanently,
    because the watermark can never come back down."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("ackwild", "windows", "one")
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_thread_read("ackwild", sender="mac", ack_through=999)
    out = await app_module.kb_thread_read("ackwild", sender="mac")
    me = next(s for s in out["floor"]["speakers"] if s["name"] == "mac")
    assert me["processed_through"] == out["floor"]["latest_seq"]

    # A later turn is still genuinely unprocessed — the bogus ack bought nothing.
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("ackwild", "windows", "two")
    view = await app_module.kb_thread_read("ackwild", sender="windows")
    mac = next(s for s in view["floor"]["others"] if s["name"] == "mac")
    assert mac["state"] == "unread"
