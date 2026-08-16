"""Floor control — whose turn it is to speak in a room.

The behaviour under test is a conversation protocol, not a data structure, so the
tests are written as the situations that actually went wrong: two sessions each
waiting for the other, and one session parked on a reply that was never coming.
"""

import sqlite3

import pytest

from engram_server.teamwork import RoomStore


def _seed_users(db_path, n: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            idp TEXT NOT NULL,
            idp_subject TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            created TEXT NOT NULL
        );
        """
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO users (handle, email, idp, idp_subject, status, created) "
            "VALUES (?, ?, 'github', ?, 'active', '2026-01-01T00:00:00Z')",
            (f"user{i}", f"user{i}@example.com", f"github:user{i}"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def rooms(tmp_path):
    db_path = tmp_path / "engram.db"
    _seed_users(db_path, 3)
    store = RoomStore(db_path)
    yield store
    store.close()


@pytest.fixture()
def room(rooms):
    """One room, two sessions of the SAME user — the configuration that broke.
    Both post as user 1, so nothing but the speaker key tells them apart."""
    r = rooms.open_room(1, "floor-test", "Take turns properly")
    rooms.touch_speaker(r.id, "sess-a", 1, name="windows")
    rooms.touch_speaker(r.id, "sess-b", 1, name="mac")
    return r


# -- the two-party case, which is nearly every room ---------------------------


def test_speaking_hands_the_floor_to_the_other_session(rooms, room):
    rooms.post_turn(room.id, 1, "over to you", speaker="sess-a")
    state = rooms.floor_state(room.id, "sess-b")
    assert state["holder"] == "sess-b"
    assert state["is_you"] is True, "the session that did NOT speak now owes a reply"


def test_the_speaker_does_not_keep_the_floor(rooms, room):
    rooms.post_turn(room.id, 1, "over to you", speaker="sess-a")
    assert rooms.floor_state(room.id, "sess-a")["is_you"] is False


def test_the_floor_comes_back_when_they_answer(rooms, room):
    rooms.post_turn(room.id, 1, "question", speaker="sess-a")
    rooms.post_turn(room.id, 1, "answer", speaker="sess-b")
    assert rooms.floor_state(room.id, "sess-a")["is_you"] is True


def test_a_speaker_key_distinguishes_two_sessions_of_one_user(rooms, room):
    """The bug underneath all of this: same user_id, same handle, two sessions."""
    rooms.post_turn(room.id, 1, "from windows", speaker="sess-a")
    rooms.post_turn(room.id, 1, "from mac", speaker="sess-b")
    state = rooms.floor_state(room.id, "sess-a")
    assert {s["name"] for s in state["speakers"]} == {"windows", "mac"}


# -- alone: the failure that costs the most -----------------------------------


def test_a_lone_session_is_told_it_is_alone(rooms):
    r = rooms.open_room(1, "empty-room", "Nobody else is here")
    rooms.touch_speaker(r.id, "sess-a", 1, name="windows")
    rooms.post_turn(r.id, 1, "anyone there?", speaker="sess-a")
    state = rooms.floor_state(r.id, "sess-a")
    assert state["alone"] is True, "waiting for a reply that cannot come is the worst outcome"


def test_alone_becomes_false_once_someone_else_appears(rooms, room):
    assert rooms.floor_state(room.id, "sess-a")["alone"] is False


def test_the_floor_stays_open_when_there_is_no_one_to_hand_it_to(rooms):
    r = rooms.open_room(1, "solo", "Alone")
    rooms.touch_speaker(r.id, "sess-a", 1)
    rooms.post_turn(r.id, 1, "hello?", speaker="sess-a")
    state = rooms.floor_state(r.id, "sess-a")
    assert state["holder"] == "", "no one to own it means open, never falsely assigned"
    assert state["open"] is True


# -- listening: 'a reply is coming' vs 'they left' -----------------------------


def test_a_listening_session_is_visible_to_the_talker(rooms, room):
    rooms.touch_speaker(room.id, "sess-b", 1, listening_seconds=60)
    assert rooms.floor_state(room.id, "sess-a")["anyone_listening"] is True


def test_an_expired_listen_does_not_count(rooms, room):
    """A session that dies mid-poll cannot clear a flag, so the window expires
    on its own. A stuck 'listening' would be worse than no signal at all."""
    rooms.touch_speaker(room.id, "sess-b", 1, listening_seconds=60)
    with rooms._lock:  # noqa: SLF001 — reach in to simulate the window lapsing
        rooms._conn.execute(
            "UPDATE room_speakers SET listening_until = '2000-01-01T00:00:00Z' "
            "WHERE room_id = ? AND speaker = ?",
            (room.id, "sess-b"),
        )
        rooms._conn.commit()
    assert rooms.floor_state(room.id, "sess-a")["anyone_listening"] is False


def test_a_satisfied_poll_stops_advertising_a_listener(rooms, room):
    """Found live: a 45s poll that returned after 4s left the window claiming a
    listener for the other 41 seconds, while that session had gone off to act on
    what it read. The other side then waits on a reply nobody is composing."""
    rooms.touch_speaker(room.id, "sess-b", 1, listening_seconds=45)
    assert rooms.floor_state(room.id, "sess-a")["anyone_listening"] is True
    rooms.stop_listening(room.id, "sess-b")
    assert rooms.floor_state(room.id, "sess-a")["anyone_listening"] is False


def test_speaking_ends_your_own_listen(rooms, room):
    rooms.touch_speaker(room.id, "sess-a", 1, listening_seconds=60)
    rooms.post_turn(room.id, 1, "actually, here's the thing", speaker="sess-a")
    assert rooms.floor_state(room.id, "sess-b")["anyone_listening"] is False


# -- surviving a reconnect ----------------------------------------------------


def test_reannouncing_a_name_absorbs_the_old_session(rooms, room):
    """A reconnect — the server restarting is enough — hands a session a brand-new
    key. Observed live: six speakers for three sessions after two restarts, with
    rotation handing the floor to keys nobody held any more."""
    rooms.post_turn(room.id, 1, "before the restart", speaker="sess-a")
    rooms.touch_speaker(room.id, "sess-a-reborn", 1, name="windows")
    names = [s["speaker"] for s in rooms.speakers(room.id)]
    assert "sess-a" not in names, "the ghost must not linger as a second participant"
    assert "sess-a-reborn" in names


def test_the_absorbed_session_keeps_its_speaking_history(rooms, room):
    """Otherwise a reconnect looks like a session that never spoke, and fair
    rotation would hand it the floor straight back."""
    rooms.post_turn(room.id, 1, "before the restart", speaker="sess-a")
    rooms.touch_speaker(room.id, "sess-a-reborn", 1, name="windows")
    reborn = next(s for s in rooms.speakers(room.id) if s["speaker"] == "sess-a-reborn")
    assert reborn["last_spoke"], "spoke-at must carry across the reconnect"


def test_the_floor_follows_a_session_through_a_reconnect(rooms, room):
    """If the floor stayed with the dead key, the room would stall waiting on a
    participant that no longer exists — while looking perfectly alive."""
    rooms.post_turn(room.id, 1, "over to you", speaker="sess-b")  # floor -> sess-a
    assert rooms.floor_state(room.id, "sess-a")["is_you"] is True
    rooms.touch_speaker(room.id, "sess-a-reborn", 1, name="windows")
    assert rooms.floor_state(room.id, "sess-a-reborn")["is_you"] is True


def test_an_unnamed_session_is_left_alone(rooms, room):
    """Only the NAME is stable identity. Sessions that never named themselves must
    not be merged with each other just for being anonymous."""
    rooms.touch_speaker(room.id, "anon-1", 1)
    rooms.touch_speaker(room.id, "anon-2", 1)
    names = [s["speaker"] for s in rooms.speakers(room.id)]
    assert "anon-1" in names and "anon-2" in names


# -- three or more, where guessing would be wrong -----------------------------


def test_hand_to_names_the_next_speaker(rooms, room):
    rooms.touch_speaker(room.id, "sess-c", 2, name="laptop")
    rooms.post_turn(room.id, 1, "mac, your call", speaker="sess-a", hand_to="mac")
    assert rooms.floor_state(room.id, "sess-b")["is_you"] is True


def test_hand_to_accepts_the_opaque_key_too(rooms, room):
    rooms.post_turn(room.id, 1, "you", speaker="sess-a", hand_to="sess-b")
    assert rooms.floor_state(room.id, "sess-b")["is_you"] is True


def test_with_three_speakers_the_floor_rotates_rather_than_opening(rooms, room):
    """An OPEN floor among three agents rebuilds the exact deadlock this exists to
    prevent: all three see 'not mine' and nobody speaks. Someone must always own it."""
    rooms.touch_speaker(room.id, "sess-c", 2, name="laptop")
    rooms.post_turn(room.id, 1, "anyone?", speaker="sess-a")
    state = rooms.floor_state(room.id, "sess-a")
    assert state["open"] is False
    assert state["holder"] in {"sess-b", "sess-c"}


def test_rotation_reaches_the_session_that_has_not_spoken(rooms, room):
    """Fair rotation: least-recently-spoken first, so a quiet session gets drawn in
    instead of two chatty ones passing it back and forth forever."""
    rooms.touch_speaker(room.id, "sess-c", 2, name="laptop")
    rooms.post_turn(room.id, 1, "one", speaker="sess-a")
    rooms.post_turn(room.id, 1, "two", speaker="sess-b")
    # a and b have both spoken; c never has, so it is next
    assert rooms.floor_state(room.id, "sess-c")["is_you"] is True


def test_the_floor_skips_a_session_that_went_home(rooms, room):
    """Handing the floor to a dead session parks the entire room behind someone
    who will never answer — the worst outcome, since it looks like progress."""
    rooms.touch_speaker(room.id, "sess-c", 2, name="laptop")
    with rooms._lock:  # noqa: SLF001 — age sess-b out of the live window
        rooms._conn.execute(
            "UPDATE room_speakers SET last_seen = '2000-01-01T00:00:00Z' "
            "WHERE room_id = ? AND speaker = ?",
            (room.id, "sess-b"),
        )
        rooms._conn.commit()
    rooms.post_turn(room.id, 1, "anyone?", speaker="sess-a")
    assert rooms.floor_state(room.id, "sess-a")["holder"] == "sess-c"


def test_when_everyone_else_is_gone_the_room_says_stalled(rooms, room):
    with rooms._lock:  # noqa: SLF001
        rooms._conn.execute(
            "UPDATE room_speakers SET last_seen = '2000-01-01T00:00:00Z' "
            "WHERE room_id = ? AND speaker = ?",
            (room.id, "sess-b"),
        )
        rooms._conn.commit()
    rooms.post_turn(room.id, 1, "still there?", speaker="sess-a")
    state = rooms.floor_state(room.id, "sess-a")
    assert state["stalled"] is True, "they were here and left — different from never here"
    assert state["alone"] is False
    assert state["open"] is True


# -- when the answer has to come from a person --------------------------------


def test_asking_the_human_takes_the_floor_away_from_every_agent(rooms, room):
    rooms.ask_human(room.id, "Do we ship this behind a flag?", asker="sess-a")
    for me in ("sess-a", "sess-b"):
        state = rooms.floor_state(room.id, me)
        assert state["is_you"] is False, f"{me} must not think it is their turn"
        assert state["holder_name"] == "the human"


def test_the_question_travels_with_the_block(rooms, room):
    rooms.ask_human(room.id, "Do we ship this behind a flag?", asker="sess-a")
    state = rooms.floor_state(room.id, "sess-b")
    assert state["awaiting_human"] == "Do we ship this behind a flag?", (
        "a session arriving mid-block must see WHAT is being asked, not just that "
        "something is"
    )


def test_a_human_answer_unblocks_and_returns_the_floor_to_the_asker(rooms, room):
    rooms.ask_human(room.id, "Flag or not?", asker="sess-a")
    rooms.post_turn(room.id, 1, "yes, behind a flag", session="dashboard:web")
    state = rooms.floor_state(room.id, "sess-a")
    assert state["awaiting_human"] == ""
    assert state["is_you"] is True, "the answer must reach whoever was waiting on it"


def test_an_agent_turn_does_not_clear_a_human_block(rooms, room):
    """Otherwise one impatient agent cancels the question another agent asked."""
    rooms.ask_human(room.id, "Flag or not?", asker="sess-a")
    rooms.post_turn(room.id, 1, "while we wait…", speaker="sess-b")
    assert rooms.floor_state(room.id, "sess-a")["awaiting_human"] == "Flag or not?"


# -- it must never get in the way ---------------------------------------------


def test_posting_out_of_turn_is_allowed(rooms, room):
    """Advisory, not a lock. Blocking a post would mean a stuck floor could
    silence a room permanently, which is far worse than talking over someone."""
    rooms.post_turn(room.id, 1, "mine", speaker="sess-a")
    assert rooms.floor_state(room.id, "sess-a")["is_you"] is False
    rooms.post_turn(room.id, 1, "mine again", speaker="sess-a")
    assert len(rooms.read_turns(room.id, 1)) == 3  # opening system turn + both


def test_a_room_with_no_speakers_still_works(rooms):
    """Old rooms, the dashboard and the web composer never register a speaker."""
    r = rooms.open_room(1, "legacy", "No floor control here")
    rooms.post_turn(r.id, 1, "plain old turn")
    state = rooms.floor_state(r.id, "")
    assert state["holder"] == ""
    assert state["alone"] is True


def test_system_turns_do_not_move_the_floor(rooms, room):
    rooms.post_turn(room.id, 1, "over to you", speaker="sess-a")
    rooms._write_turn_locked  # noqa: B018 — documented internal, used by close_room
    before = rooms.floor_state(room.id, "sess-b")["holder"]
    rooms.post_turn(room.id, 1, "a system note", speaker="sess-a", kind="system")
    assert rooms.floor_state(room.id, "sess-b")["holder"] == before
