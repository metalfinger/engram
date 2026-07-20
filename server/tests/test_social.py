"""M2.1 — SocialStore: contacts, DMs/groups, notifications (multi-user social layer)."""

import sqlite3

import pytest

from engram_server.social import SocialError, SocialStore


def _seed_users(db_path, n: int) -> None:
    """Insert minimal users rows directly so social.py's FKs (declared but not
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
def social(tmp_path):
    db_path = tmp_path / "engram.db"
    _seed_users(db_path, 6)
    store = SocialStore(db_path)
    yield store
    store.close()


# -- contacts -----------------------------------------------------------------


def test_request_contact_creates_pending(social):
    c = social.request_contact(1, 2)
    assert c.status == "pending"
    assert c.requester_id == 1
    assert c.addressee_id == 2
    contacts = social.list_contacts(2)
    assert contacts["incoming"] == [1]
    assert social.list_contacts(1)["outgoing"] == [2]


def test_reverse_request_auto_accepts(social):
    social.request_contact(1, 2)
    reverse = social.request_contact(2, 1)
    assert reverse.status == "accepted"
    assert social.are_contacts(1, 2)
    assert social.are_contacts(2, 1)


def test_accept_contact(social):
    social.request_contact(1, 3)
    accepted = social.accept_contact(3, 1)
    assert accepted.status == "accepted"
    assert social.are_contacts(1, 3)


def test_accept_contact_requires_pending_request(social):
    with pytest.raises(SocialError):
        social.accept_contact(3, 1)


def test_accept_contact_only_addressee_may_accept(social):
    social.request_contact(1, 3)
    # 1 is the requester, not the addressee — no pending request FROM 3 TO 1 exists.
    with pytest.raises(SocialError):
        social.accept_contact(1, 3)


def test_duplicate_same_direction_request_is_idempotent(social):
    first = social.request_contact(1, 2)
    again = social.request_contact(1, 2)
    assert first.id == again.id
    assert again.status == "pending"


def test_self_contact_rejected(social):
    with pytest.raises(SocialError, match="yourself"):
        social.request_contact(1, 1)


def test_already_contacts_rejected(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    with pytest.raises(SocialError, match="already contacts"):
        social.request_contact(1, 2)
    with pytest.raises(SocialError, match="already contacts"):
        social.request_contact(2, 1)


def test_list_contacts_shape(social):
    social.request_contact(1, 2)
    social.request_contact(1, 3)
    social.accept_contact(3, 1)
    social.request_contact(4, 1)
    result = social.list_contacts(1)
    assert result["accepted"] == [3]
    assert result["outgoing"] == [2]
    assert result["incoming"] == [4]


# -- DMs ------------------------------------------------------------------


def test_dm_requires_contacts(social):
    with pytest.raises(SocialError, match="[Nn]ot contacts"):
        social.get_or_create_dm(1, 2)


def test_dm_created_once_contacts(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    assert conv.kind == "dm"


def test_dm_is_deterministic_single_conversation_per_pair(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv_a = social.get_or_create_dm(1, 2)
    conv_b = social.get_or_create_dm(2, 1)
    assert conv_a.id == conv_b.id


# -- groups -----------------------------------------------------------------


def test_create_group_includes_creator_and_dedups(social):
    conv = social.create_group(1, "Project Chat", [2, 3, 1])
    assert conv.kind == "group"
    assert conv.title == "Project Chat"
    convs = social.list_conversations(1)
    members = next(c for c in convs if c["conv_id"] == conv.id)["members"]
    assert sorted(members) == [1, 2, 3]


def test_create_group_requires_title(social):
    with pytest.raises(SocialError):
        social.create_group(1, "", [2, 3])


def test_create_group_no_contact_requirement(social):
    # No pre-existing contact relation between 1 and 5 — groups don't gate on it.
    conv = social.create_group(1, "Open Group", [5])
    assert conv.kind == "group"


# -- messages ---------------------------------------------------------------


def test_send_message_requires_membership(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    with pytest.raises(SocialError, match="not a member"):
        social.send_message(conv.id, 3, "hi")


def test_send_message_rejects_empty_and_oversize(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    with pytest.raises(SocialError):
        social.send_message(conv.id, 1, "")
    with pytest.raises(SocialError):
        social.send_message(conv.id, 1, "x" * 4001)
    # exactly at the limit is fine
    msg = social.send_message(conv.id, 1, "x" * 4000)
    assert len(msg.body) == 4000


def test_send_and_list_messages(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    social.send_message(conv.id, 1, "hello")
    social.send_message(conv.id, 2, "hi back")
    msgs = social.list_messages(conv.id, 1)
    assert [m.body for m in msgs] == ["hello", "hi back"]


def test_list_messages_requires_membership(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    with pytest.raises(SocialError, match="not a member"):
        social.list_messages(conv.id, 3)


def test_list_messages_since_and_limit(social):
    # created has second resolution, so within-a-test-run timestamps can tie;
    # backdate "one" directly so "since" has an unambiguous boundary to filter on.
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    one = social.send_message(conv.id, 1, "one")
    social._conn.execute(
        "UPDATE messages SET created = '2020-01-01T00:00:00Z' WHERE id = ?", (one.id,)
    )
    social._conn.commit()
    cutoff = "2020-01-01T00:00:00Z"
    social.send_message(conv.id, 2, "two")
    social.send_message(conv.id, 1, "three")
    since_msgs = social.list_messages(conv.id, 1, since=cutoff)
    assert [m.body for m in since_msgs] == ["two", "three"]
    limited = social.list_messages(conv.id, 1, limit=1)
    assert len(limited) == 1
    assert limited[0].body == "one"


# -- unread + read markers ---------------------------------------------------


def test_unread_counts_and_mark_read(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    social.send_message(conv.id, 2, "hey")
    social.send_message(conv.id, 2, "you there?")

    convs = social.list_conversations(1)
    this_conv = next(c for c in convs if c["conv_id"] == conv.id)
    assert this_conv["unread"] == 2
    assert this_conv["last_message"].body == "you there?"

    counts = social.unread_counts(1)
    assert counts["dms"] == 2

    social.mark_read(conv.id, 1)
    convs_after = social.list_conversations(1)
    this_conv_after = next(c for c in convs_after if c["conv_id"] == conv.id)
    assert this_conv_after["unread"] == 0
    assert social.unread_counts(1)["dms"] == 0


def test_mark_read_requires_membership(social):
    social.request_contact(1, 2)
    social.accept_contact(2, 1)
    conv = social.get_or_create_dm(1, 2)
    with pytest.raises(SocialError, match="not a member"):
        social.mark_read(conv.id, 3)


# -- notifications ------------------------------------------------------------


def test_create_and_list_notifications(social):
    n1 = social.create_notification(1, "contact_request", "user2 wants to connect", ref="2")
    n2 = social.create_notification(1, "message", "new DM from user2", ref=str(1))
    notifs = social.list_notifications(1)
    assert [n.id for n in notifs] == [n2.id, n1.id]  # newest first
    assert not notifs[0].read
    assert n1.ref == "2"


def test_list_notifications_unread_only(social):
    social.create_notification(1, "system", "hello")
    n2 = social.create_notification(1, "system", "world")
    social.mark_notifications_read(1, [n2.id])
    unread = social.list_notifications(1, unread_only=True)
    assert len(unread) == 1
    assert unread[0].body == "hello"


def test_mark_notifications_read_specific_ids(social):
    n1 = social.create_notification(1, "system", "a")
    social.create_notification(1, "system", "b")
    changed = social.mark_notifications_read(1, [n1.id])
    assert changed == 1
    notifs = {n.id: n.read for n in social.list_notifications(1)}
    assert notifs[n1.id] is True


def test_mark_notifications_read_all(social):
    social.create_notification(1, "system", "a")
    social.create_notification(1, "system", "b")
    changed = social.mark_notifications_read(1, ids=None)
    assert changed == 2
    assert all(n.read for n in social.list_notifications(1))
    # idempotent — nothing left to mark
    assert social.mark_notifications_read(1, ids=None) == 0


def test_unread_counts_includes_notifications(social):
    social.create_notification(1, "system", "a")
    counts = social.unread_counts(1)
    assert counts["notifications"] == 1
    assert counts["dms"] == 0
