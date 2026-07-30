"""M5.2 — DiscoveryStore: follows + asks (discovery layer for multi-user Engram)."""

import sqlite3

import pytest

from engram_server.discovery import DiscoveryError, DiscoveryStore


def _seed_users(db_path, n: int) -> None:
    """Insert minimal users rows directly so discovery.py's FKs (declared but not
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
def discovery(tmp_path):
    db_path = tmp_path / "engram.db"
    _seed_users(db_path, 6)
    store = DiscoveryStore(db_path)
    yield store
    store.close()


# -- follows ------------------------------------------------------------------


def test_follow_creates_row(discovery):
    f = discovery.follow(1, 2)
    assert f.follower_id == 1
    assert f.followee_id == 2
    assert discovery.is_following(1, 2)


def test_follow_is_idempotent(discovery):
    first = discovery.follow(1, 2)
    again = discovery.follow(1, 2)
    assert first.id == again.id


def test_self_follow_rejected(discovery):
    with pytest.raises(DiscoveryError, match="yourself"):
        discovery.follow(1, 1)


def test_unfollow_removes_relation(discovery):
    discovery.follow(1, 2)
    discovery.unfollow(1, 2)
    assert not discovery.is_following(1, 2)


def test_unfollow_twice_is_not_an_error(discovery):
    # Idempotent by design — unfollowing an already-not-followed user is a no-op,
    # not a failure. The caller wanted "not following"; that's already true.
    discovery.follow(1, 2)
    discovery.unfollow(1, 2)
    discovery.unfollow(1, 2)  # second call — must not raise
    assert not discovery.is_following(1, 2)


def test_unfollow_when_never_followed_is_a_no_op(discovery):
    discovery.unfollow(1, 2)  # never followed — still must not raise
    assert not discovery.is_following(1, 2)


def test_is_following_is_asymmetric(discovery):
    # This is the whole difference from social.py's mutual contacts: A
    # following B says nothing about B following A.
    discovery.follow(1, 2)
    assert discovery.is_following(1, 2)
    assert not discovery.is_following(2, 1)


def test_followers_and_following_lists(discovery):
    discovery.follow(1, 2)
    discovery.follow(3, 2)
    discovery.follow(2, 4)
    assert discovery.followers(2) == [1, 3]
    assert discovery.following(2) == [4]
    assert discovery.followers(4) == [2]
    assert discovery.following(1) == [2]


def test_follow_counts(discovery):
    discovery.follow(1, 2)
    discovery.follow(3, 2)
    discovery.follow(2, 4)
    counts = discovery.follow_counts(2)
    assert counts == {"followers": 2, "following": 1}


# -- asks -----------------------------------------------------------------


def test_create_and_get_ask(discovery):
    ask = discovery.create_ask(1, 2, "projects/foo/context.md", "What's the status here?")
    assert ask.asker_id == 1
    assert ask.owner_id == 2
    assert ask.path == "projects/foo/context.md"
    assert ask.status == "open"
    assert ask.answer is None
    fetched = discovery.get_ask(ask.id)
    assert fetched == ask


def test_get_ask_unknown_returns_none(discovery):
    assert discovery.get_ask(999) is None


def test_self_ask_rejected(discovery):
    with pytest.raises(DiscoveryError, match="yourself"):
        discovery.create_ask(1, 1, "projects/foo/context.md", "hm?")


def test_empty_or_whitespace_question_rejected(discovery):
    with pytest.raises(DiscoveryError):
        discovery.create_ask(1, 2, "projects/foo/context.md", "")
    with pytest.raises(DiscoveryError):
        discovery.create_ask(1, 2, "projects/foo/context.md", "   ")


def test_oversize_question_rejected(discovery):
    with pytest.raises(DiscoveryError):
        discovery.create_ask(1, 2, "projects/foo/context.md", "x" * 2001)
    # exactly at the limit is fine
    ask = discovery.create_ask(1, 2, "projects/foo/context.md", "x" * 2000)
    assert len(ask.question) == 2000


def test_answer_ask_by_owner(discovery):
    ask = discovery.create_ask(1, 2, "projects/foo/context.md", "What's the status here?")
    answered = discovery.answer_ask(ask.id, 2, "It's on track.")
    assert answered.status == "answered"
    assert answered.answer == "It's on track."
    assert answered.answered_at is not None


def test_answer_ask_by_non_owner_rejected(discovery):
    # Security-critical: only the owner may answer their own asks.
    ask = discovery.create_ask(1, 2, "projects/foo/context.md", "What's the status here?")
    with pytest.raises(DiscoveryError, match="owner"):
        discovery.answer_ask(ask.id, 1, "Nope, not the owner.")
    with pytest.raises(DiscoveryError, match="owner"):
        discovery.answer_ask(ask.id, 3, "Also not the owner.")
    # still open — the rejected attempts left it untouched
    assert discovery.get_ask(ask.id).status == "open"


def test_answer_unknown_ask_rejected(discovery):
    with pytest.raises(DiscoveryError):
        discovery.answer_ask(999, 2, "answer")


def test_double_answer_rejected(discovery):
    ask = discovery.create_ask(1, 2, "projects/foo/context.md", "What's the status here?")
    discovery.answer_ask(ask.id, 2, "First answer.")
    with pytest.raises(DiscoveryError):
        discovery.answer_ask(ask.id, 2, "Second answer.")


def test_empty_or_oversize_answer_rejected(discovery):
    ask = discovery.create_ask(1, 2, "projects/foo/context.md", "What's the status here?")
    with pytest.raises(DiscoveryError):
        discovery.answer_ask(ask.id, 2, "")
    with pytest.raises(DiscoveryError):
        discovery.answer_ask(ask.id, 2, "x" * 4001)
    # exactly at the limit is fine
    answered = discovery.answer_ask(ask.id, 2, "x" * 4000)
    assert len(answered.answer) == 4000


def test_list_asks_for_open_only_filtering_and_ordering(discovery):
    a1 = discovery.create_ask(1, 2, "projects/foo/context.md", "first?")
    a2 = discovery.create_ask(3, 2, "projects/foo/log.md", "second?")
    discovery.answer_ask(a1.id, 2, "answered")
    open_only = discovery.list_asks_for(2, open_only=True)
    assert [a.id for a in open_only] == [a2.id]
    everything = discovery.list_asks_for(2, open_only=False)
    assert [a.id for a in everything] == [a2.id, a1.id]  # newest first


def test_list_asks_by(discovery):
    a1 = discovery.create_ask(1, 2, "projects/foo/context.md", "one?")
    a2 = discovery.create_ask(1, 3, "projects/bar/context.md", "two?")
    mine = discovery.list_asks_by(1)
    assert [a.id for a in mine] == [a2.id, a1.id]  # newest first
    assert discovery.list_asks_by(4) == []


def test_ask_counts(discovery):
    a1 = discovery.create_ask(1, 2, "projects/foo/context.md", "one?")
    discovery.create_ask(3, 2, "projects/foo/log.md", "two?")
    assert discovery.ask_counts(2) == {"open_for_me": 2}
    discovery.answer_ask(a1.id, 2, "answered")
    assert discovery.ask_counts(2) == {"open_for_me": 1}
