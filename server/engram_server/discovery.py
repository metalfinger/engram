"""Discovery store — follows + asks for multi-user Engram (v2 M5.2).

Users can mark parts of their brain public; other users discover them,
FOLLOW them (one-directional — unlike social.py's mutual-consent contacts),
and ASK a question about a specific piece of someone's public work. Like
social.py this lives in the SAME neutral SQLite file (engram.db) as a
sibling table set referencing users(id) by plain INTEGER.

The security point of this module: an ask is quarantine. A question from
another user must NEVER be written into the owner's git brain — it sits in
this DB (`status='open'`) until the owner reads it and chooses, themselves,
what (if anything) to do about it. `create_ask` stores `path` verbatim and
does not check that it is actually public — this module is MECHANISM only;
whether `path` is public is the CALLER's (tool-layer) policy to enforce,
mirroring social.send_message's secret-scan boundary note.

Sync API by design, same as SocialStore/TenancyStore: microsecond-scale row
operations guarded by one process-wide lock, safe to call from async code
without a threadpool.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import KBError

_MAX_QUESTION_LEN = 2000
_MAX_ANSWER_LEN = 4000


class DiscoveryError(KBError):
    """Follow/ask misuse; the message teaches the caller the correct next step."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Follow:
    id: int
    follower_id: int
    followee_id: int
    created: str


@dataclass(frozen=True)
class Ask:
    id: int
    asker_id: int
    owner_id: int
    path: str
    question: str
    answer: str | None
    status: str  # open | answered
    created: str
    answered_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    follower_id INTEGER NOT NULL REFERENCES users(id),
    followee_id INTEGER NOT NULL REFERENCES users(id),
    created TEXT NOT NULL,
    UNIQUE(follower_id, followee_id)
);
CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asker_id INTEGER NOT NULL REFERENCES users(id),
    owner_id INTEGER NOT NULL REFERENCES users(id),
    path TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created TEXT NOT NULL,
    answered_at TEXT
);
"""


class DiscoveryStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- row -> dataclass helpers ---------------------------------------------

    def _follow(self, row: sqlite3.Row | None) -> Follow | None:
        if row is None:
            return None
        return Follow(
            id=row["id"], follower_id=row["follower_id"],
            followee_id=row["followee_id"], created=row["created"],
        )

    def _ask(self, row: sqlite3.Row | None) -> Ask | None:
        if row is None:
            return None
        return Ask(
            id=row["id"], asker_id=row["asker_id"], owner_id=row["owner_id"],
            path=row["path"], question=row["question"], answer=row["answer"],
            status=row["status"], created=row["created"],
            answered_at=row["answered_at"],
        )

    # -- follows (one-directional — no consent gate, unlike contacts) --------

    def follow(self, follower_id: int, followee_id: int) -> Follow:
        if follower_id == followee_id:
            raise DiscoveryError("You can't follow yourself.")
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM follows WHERE follower_id = ? AND followee_id = ?",
                (follower_id, followee_id),
            ).fetchone()
            if existing is not None:
                return self._follow(existing)  # type: ignore[return-value]
            cur = self._conn.execute(
                "INSERT INTO follows (follower_id, followee_id, created) VALUES (?, ?, ?)",
                (follower_id, followee_id, _now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM follows WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._follow(row)  # type: ignore[return-value]

    def unfollow(self, follower_id: int, followee_id: int) -> None:
        """Silent no-op if not following. Idempotent by design: unfollowing
        twice is not an error — the caller wanted "not following" and that's
        the end state either way, so there's nothing to teach them about."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM follows WHERE follower_id = ? AND followee_id = ?",
                (follower_id, followee_id),
            )
            self._conn.commit()

    def is_following(self, follower_id: int, followee_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?",
                (follower_id, followee_id),
            ).fetchone()
        return row is not None

    def followers(self, user_id: int) -> list[int]:
        """People who follow user_id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT follower_id FROM follows WHERE followee_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [r["follower_id"] for r in rows]

    def following(self, user_id: int) -> list[int]:
        """People user_id follows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT followee_id FROM follows WHERE follower_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [r["followee_id"] for r in rows]

    def follow_counts(self, user_id: int) -> dict:
        with self._lock:
            followers_n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM follows WHERE followee_id = ?", (user_id,)
            ).fetchone()["n"]
            following_n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM follows WHERE follower_id = ?", (user_id,)
            ).fetchone()["n"]
        return {"followers": followers_n, "following": following_n}

    # -- asks (quarantined until the owner answers — never git) --------------

    def create_ask(self, asker_id: int, owner_id: int, path: str, question: str) -> Ask:
        """`path` is the repo-relative path in the OWNER's brain the question is
        about, stored verbatim. This store does not check that `path` is
        actually public — the CALLER must enforce that before calling here."""
        if asker_id == owner_id:
            raise DiscoveryError("You can't ask yourself a question.")
        question = (question or "").strip()
        if not question:
            raise DiscoveryError("Question cannot be empty.")
        if len(question) > _MAX_QUESTION_LEN:
            raise DiscoveryError(f"Question exceeds {_MAX_QUESTION_LEN} characters.")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO asks (asker_id, owner_id, path, question, status, created) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (asker_id, owner_id, path, question, _now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM asks WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._ask(row)  # type: ignore[return-value]

    def answer_ask(self, ask_id: int, owner_id: int, answer: str) -> Ask:
        answer = (answer or "").strip()
        if not answer:
            raise DiscoveryError("Answer cannot be empty.")
        if len(answer) > _MAX_ANSWER_LEN:
            raise DiscoveryError(f"Answer exceeds {_MAX_ANSWER_LEN} characters.")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
            ask = self._ask(row)
            if ask is None:
                raise DiscoveryError(f"No ask with id {ask_id}.")
            if ask.owner_id != owner_id:
                raise DiscoveryError("Only the owner of the ask may answer it.")
            if ask.status != "open":
                raise DiscoveryError(f"Ask {ask_id} is already {ask.status}, not open.")
            now = _now()
            self._conn.execute(
                "UPDATE asks SET answer = ?, status = 'answered', answered_at = ? WHERE id = ?",
                (answer, now, ask_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
            return self._ask(row)  # type: ignore[return-value]

    def get_ask(self, ask_id: int) -> Ask | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
        return self._ask(row)

    def list_asks_for(self, owner_id: int, open_only: bool = True, limit: int = 50) -> list[Ask]:
        """Questions I should answer, newest first."""
        with self._lock:
            if open_only:
                rows = self._conn.execute(
                    "SELECT * FROM asks WHERE owner_id = ? AND status = 'open' "
                    "ORDER BY id DESC LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM asks WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
        return [self._ask(r) for r in rows]  # type: ignore[misc]

    def list_asks_by(self, asker_id: int, limit: int = 50) -> list[Ask]:
        """Questions I asked, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM asks WHERE asker_id = ? ORDER BY id DESC LIMIT ?",
                (asker_id, limit),
            ).fetchall()
        return [self._ask(r) for r in rows]  # type: ignore[misc]

    def ask_counts(self, user_id: int) -> dict:
        """How many unanswered questions await this user."""
        with self._lock:
            open_n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM asks WHERE owner_id = ? AND status = 'open'",
                (user_id,),
            ).fetchone()["n"]
        return {"open_for_me": open_n}
