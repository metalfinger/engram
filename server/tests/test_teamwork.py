"""Rooms + presence — cross-brain live joins for multi-user Engram."""

import sqlite3

import pytest

from engram_server.teamwork import PresenceStore, RoomStore, TeamworkError


def _seed_users(db_path, n: int) -> None:
    """Insert minimal users rows directly so teamwork.py's FKs (declared but not
    enforced-by-creation in SQLite) are satisfiable by real ids 1..n."""
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
def presence(tmp_path):
    db_path = tmp_path / "engram.db"
    _seed_users(db_path, 3)
    store = PresenceStore(db_path)
    yield store
    store.close()


# -- open_room ----------------------------------------------------------------


def test_open_room_requires_goal(rooms):
    with pytest.raises(TeamworkError, match="goal"):
        rooms.open_room(1, "planning", "")
    with pytest.raises(TeamworkError, match="goal"):
        rooms.open_room(1, "planning", "   ")


def test_open_room_slug_lowercased(rooms):
    room = rooms.open_room(1, "PLAN-Room", "ship it")
    assert room.name == "plan-room"
    assert rooms.room_by_name("PLAN-Room") is not None
    assert rooms.room_by_name("plan-room").id == room.id


def test_open_room_bad_slugs_rejected(rooms):
    for bad in ("ab", "-abc", "abc_def", "Abc Def", "", "a" * 65):
        with pytest.raises(TeamworkError):
            rooms.open_room(1, bad, "goal")


def test_open_room_duplicate_name_mentions_suffix(rooms):
    rooms.open_room(1, "planning", "ship it")
    with pytest.raises(TeamworkError, match="suffix"):
        rooms.open_room(2, "planning", "ship it too")


def test_open_room_budget_bounds(rooms):
    with pytest.raises(TeamworkError):
        rooms.open_room(1, "planning", "goal", turn_budget=0, hard_cap=10)
    with pytest.raises(TeamworkError):
        rooms.open_room(1, "planning", "goal", turn_budget=10, hard_cap=5)
    with pytest.raises(TeamworkError):
        rooms.open_room(1, "planning", "goal", turn_budget=10, hard_cap=501)


def test_creator_is_member_and_system_turn_written(rooms):
    room = rooms.open_room(1, "planning", "ship the thing")
    members = rooms.members(room.id)
    assert [m["user_id"] for m in members] == [1]
    turns = rooms.read_turns(room.id, 1)
    assert len(turns) == 1
    assert turns[0].kind == "system"
    assert "ship the thing" in turns[0].body


# -- membership gating ----------------------------------------------------


def test_non_member_cannot_post_read_invite_close_grant(rooms):
    room = rooms.open_room(1, "planning", "goal")
    with pytest.raises(TeamworkError, match="not a member"):
        rooms.post_turn(room.id, 2, "hello")
    with pytest.raises(TeamworkError, match="not a member"):
        rooms.read_turns(room.id, 2)
    with pytest.raises(TeamworkError, match="not a member"):
        rooms.invite(room.id, 2, 3)
    with pytest.raises(TeamworkError, match="not a member"):
        rooms.close_room(room.id, 2)
    with pytest.raises(TeamworkError, match="not a member"):
        rooms.add_grant(room.id, 2, "projects/slate")


def test_invite_idempotent_and_self_invite_noop(rooms):
    room = rooms.open_room(1, "planning", "goal")
    assert rooms.invite(room.id, 1, 2) is True
    assert rooms.invite(room.id, 1, 2) is False
    assert rooms.invite(room.id, 1, 1) is False
    member_ids = {m["user_id"] for m in rooms.members(room.id)}
    assert member_ids == {1, 2}


def test_invited_member_can_post(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.invite(room.id, 1, 2)
    turn = rooms.post_turn(room.id, 2, "hi from 2")
    assert turn.body == "hi from 2"
    assert turn.kind == "message"


# -- budgets ----------------------------------------------------------------


def test_budget_refusal_mentions_extend(rooms):
    room = rooms.open_room(1, "planning", "goal", turn_budget=2, hard_cap=10)
    rooms.post_turn(room.id, 1, "one")
    rooms.post_turn(room.id, 1, "two")
    with pytest.raises(TeamworkError, match="extend"):
        rooms.post_turn(room.id, 1, "three")
    rooms.extend_budget(room.id, 1, 5)
    turn = rooms.post_turn(room.id, 1, "three now fits")
    assert turn.body == "three now fits"


def test_hard_cap_refusal_differs_from_budget_refusal(rooms):
    room = rooms.open_room(1, "planning", "goal", turn_budget=2, hard_cap=2)
    rooms.post_turn(room.id, 1, "one")
    rooms.post_turn(room.id, 1, "two")
    with pytest.raises(TeamworkError, match="hard cap") as exc:
        rooms.post_turn(room.id, 1, "three")
    assert "extend" not in str(exc.value)


def test_system_and_guest_read_turns_never_blocked_by_budget(rooms):
    room = rooms.open_room(1, "planning", "goal", turn_budget=1, hard_cap=1)
    # the creator's "room opened" system turn already consumed nothing against budget
    rooms.post_turn(room.id, 1, "uses the one message slot")
    with pytest.raises(TeamworkError):
        rooms.post_turn(room.id, 1, "over budget")
    # but system/guest_read turns still go through
    turn = rooms.post_turn(room.id, 1, "a system note", kind="system")
    assert turn.kind == "system"
    turn2 = rooms.post_turn(room.id, 1, "a guest read", kind="guest_read")
    assert turn2.kind == "guest_read"


def test_extend_budget_capped_at_hard_cap(rooms):
    room = rooms.open_room(1, "planning", "goal", turn_budget=2, hard_cap=5)
    updated = rooms.extend_budget(room.id, 1, 100)
    assert updated.turn_budget == 5


def test_extend_budget_requires_member_and_open(rooms):
    room = rooms.open_room(1, "planning", "goal", turn_budget=2, hard_cap=5)
    with pytest.raises(TeamworkError, match="not a member"):
        rooms.extend_budget(room.id, 2, 1)
    rooms.close_room(room.id, 1)
    with pytest.raises(TeamworkError):
        rooms.extend_budget(room.id, 1, 1)


# -- read cursor + unread ------------------------------------------------------


def test_read_turns_since_id_cursor(rooms):
    room = rooms.open_room(1, "planning", "goal")
    t1 = rooms.post_turn(room.id, 1, "one")
    t2 = rooms.post_turn(room.id, 1, "two")
    from_start = rooms.read_turns(room.id, 1, since_id=0)
    assert [t.body for t in from_start] == ["room opened: goal", "one", "two"]
    since_t1 = rooms.read_turns(room.id, 1, since_id=t1.id)
    assert [t.body for t in since_t1] == ["two"]
    since_t2 = rooms.read_turns(room.id, 1, since_id=t2.id)
    assert since_t2 == []


def test_unread_drops_to_zero_after_read_turns(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.invite(room.id, 1, 2)
    rooms.post_turn(room.id, 1, "one")
    rooms.post_turn(room.id, 1, "two")
    listing = rooms.list_rooms_for(2)
    this_room = next(r for r in listing if r["id"] == room.id)
    assert this_room["unread"] == 3  # system open + two messages, user2 never read
    rooms.read_turns(room.id, 2)
    listing_after = rooms.list_rooms_for(2)
    this_room_after = next(r for r in listing_after if r["id"] == room.id)
    assert this_room_after["unread"] == 0


def test_list_rooms_for_messages_used_and_last_turn(rooms):
    room = rooms.open_room(1, "planning", "goal", turn_budget=10, hard_cap=20)
    rooms.invite(room.id, 1, 2)
    # only "message" kind turns count toward messages_used
    rooms.post_turn(room.id, 1, "one")
    rooms.post_turn(room.id, 1, "two")
    rooms.post_turn(room.id, 1, "a system note", kind="system")

    listing = rooms.list_rooms_for(1)
    this_room = next(r for r in listing if r["id"] == room.id)
    assert this_room["messages_used"] == 2  # system turns excluded, "room opened" excluded too
    assert this_room["last_turn"] == {
        "author_id": 1,
        "kind": "system",
        "body": "a system note",
        "created": this_room["last_turn"]["created"],
    }


def test_list_rooms_for_last_turn_body_truncated_to_200(rooms):
    room = rooms.open_room(1, "planning", "goal")
    long_body = "x" * 4000
    rooms.post_turn(room.id, 1, long_body)
    listing = rooms.list_rooms_for(1)
    this_room = next(r for r in listing if r["id"] == room.id)
    assert this_room["last_turn"]["body"] == "x" * 200


def test_list_rooms_for_last_turn_none_when_room_has_no_turns(rooms):
    # unreachable in practice (open_room always writes a system turn), but the
    # contract says last_turn can be None — cover it directly against the store.
    with rooms._lock:
        rooms._conn.execute(
            "INSERT INTO rooms (name, creator_id, goal, exit_condition, turn_budget, "
            "hard_cap, status, outcome, created, closed_at) "
            "VALUES ('empty-room', 1, 'goal', '', 40, 200, 'open', NULL, '2026-01-01T00:00:00Z', NULL)"
        )
        room_id = rooms._conn.execute(
            "SELECT id FROM rooms WHERE name = 'empty-room'"
        ).fetchone()["id"]
        rooms._conn.execute(
            "INSERT INTO room_members (room_id, user_id, invited_by, joined) "
            "VALUES (?, 1, NULL, '2026-01-01T00:00:00Z')",
            (room_id,),
        )
        rooms._conn.commit()
    listing = rooms.list_rooms_for(1)
    this_room = next(r for r in listing if r["id"] == room_id)
    assert this_room["last_turn"] is None
    assert this_room["messages_used"] == 0


def test_last_turn_id(rooms):
    room = rooms.open_room(1, "planning", "goal")
    assert rooms.last_turn_id(room.id) >= 1
    t = rooms.post_turn(room.id, 1, "hi")
    assert rooms.last_turn_id(room.id) == t.id


# -- close_room ---------------------------------------------------------------


def test_close_room_stores_outcome_and_blocks_further_posts(rooms):
    room = rooms.open_room(1, "planning", "goal")
    closed = rooms.close_room(room.id, 1, outcome="shipped it")
    assert closed.status == "closed"
    assert closed.outcome == "shipped it"
    assert closed.closed_at is not None
    with pytest.raises(TeamworkError, match="closed"):
        rooms.post_turn(room.id, 1, "too late")


def test_close_room_idempotent_error(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.close_room(room.id, 1)
    with pytest.raises(TeamworkError, match="already closed"):
        rooms.close_room(room.id, 1)


def test_grants_for_empty_after_close(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.add_grant(room.id, 1, "projects/slate")
    assert rooms.grants_for(room.id) != []
    rooms.close_room(room.id, 1)
    assert rooms.grants_for(room.id) == []


# -- grants -------------------------------------------------------------------


def test_grant_rejects_unshareable_segments(rooms):
    room = rooms.open_room(1, "planning", "goal")
    for bad_path in (
        "projects/slate/messages/x.md",
        "inbox/foo",
        "workspace/bar",
        "threads/baz",
        "projects/.git/config",
    ):
        with pytest.raises(TeamworkError):
            rooms.add_grant(room.id, 1, bad_path)


def test_grant_rejects_dotdot(rooms):
    room = rooms.open_room(1, "planning", "goal")
    with pytest.raises(TeamworkError):
        rooms.add_grant(room.id, 1, "projects/../secrets")


def test_assert_grant_prefix_boundary(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.add_grant(room.id, 1, "projects/slate")
    # covered: exact + nested
    rooms.assert_grant(room.id, 1, "projects/slate")
    rooms.assert_grant(room.id, 1, "projects/slate/x.md")
    # NOT covered: sibling with shared prefix string but different path segment
    with pytest.raises(TeamworkError):
        rooms.assert_grant(room.id, 1, "projects/slate-2/x.md")


def test_assert_grant_fails_for_non_granted_path_and_closed_room(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.add_grant(room.id, 1, "projects/slate")
    with pytest.raises(TeamworkError):
        rooms.assert_grant(room.id, 1, "projects/other")
    rooms.close_room(room.id, 1)
    with pytest.raises(TeamworkError):
        rooms.assert_grant(room.id, 1, "projects/slate")


def test_revoke_grant(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.add_grant(room.id, 1, "projects/slate")
    rooms.revoke_grant(room.id, 1, "projects/slate")
    assert rooms.grants_for(room.id) == []


def test_add_grant_requires_open_room(rooms):
    room = rooms.open_room(1, "planning", "goal")
    rooms.close_room(room.id, 1)
    with pytest.raises(TeamworkError):
        rooms.add_grant(room.id, 1, "projects/slate")


# -- presence -----------------------------------------------------------------


def test_touch_preserves_project_when_none(presence):
    presence.touch(1, tool="claude-code", project="engram")
    presence.touch(1, tool="claude-code")
    row = presence.self_row(1)
    assert row["project"] == "engram"
    assert row["tool"] == "claude-code"


def test_touch_sets_project_when_given(presence):
    presence.touch(1, tool="claude-code", project="engram")
    presence.touch(1, tool="claude-code", project="metalfinger")
    row = presence.self_row(1)
    assert row["project"] == "metalfinger"


def test_roster_excludes_invisible(presence):
    presence.touch(1, tool="x", project="p1")
    presence.touch(2, tool="x", project="p2")
    presence.set_invisible(2, True)
    roster = presence.roster()
    ids = {r["user_id"] for r in roster}
    assert 1 in ids
    assert 2 not in ids


def test_roster_excludes_stale(presence, tmp_path):
    presence.touch(1, tool="x", project="p1")
    presence.touch(2, tool="x", project="p2")
    # backdate user 2's row far outside the active window
    presence._conn.execute(
        "UPDATE team_presence SET updated = '2020-01-01T00:00:00Z' WHERE user_id = ?", (2,)
    )
    presence._conn.commit()
    roster = presence.roster(active_minutes=120)
    ids = {r["user_id"] for r in roster}
    assert 1 in ids
    assert 2 not in ids


def test_self_row_returns_invisible_row(presence):
    presence.touch(1, tool="x", project="p1")
    presence.set_invisible(1, True)
    row = presence.self_row(1)
    assert row is not None
    assert row["invisible"] is True
    roster = presence.roster()
    assert row["user_id"] not in {r["user_id"] for r in roster}
