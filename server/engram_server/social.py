"""Social store — contacts, DMs/groups, notifications for multi-user Engram (v2 M2.1).

A DM between user A and user B cannot live inside either user's isolated git
brain, so cross-user social state lives in the SAME neutral SQLite file
(engram.db) as tenancy.py, as a sibling table set referencing users(id) by
plain INTEGER. This module is MECHANISM only: it enforces integrity (mutual
consent via the contacts gate, membership on every conversation op, message
size limits) but leaves content policy — secret-scanning message bodies —
to the caller. See send_message for that boundary.

Sync API by design, same as TenancyStore: microsecond-scale row operations
guarded by one process-wide lock, safe to call from async code without a
threadpool.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import KBError

_MAX_MESSAGE_LEN = 4000


class SocialError(KBError):
    """Contact/conversation/notification misuse; the message teaches the correct next step."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Contact:
    id: int
    requester_id: int
    addressee_id: int
    status: str  # pending | accepted
    created: str


@dataclass(frozen=True)
class Conversation:
    id: int
    kind: str  # dm | group
    title: str | None
    created: str


@dataclass(frozen=True)
class Message:
    id: int
    conv_id: int
    sender_id: int
    body: str
    created: str


@dataclass(frozen=True)
class Notification:
    id: int
    user_id: int
    kind: str
    body: str
    ref: str | None
    read: bool
    created: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    addressee_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created TEXT NOT NULL,
    UNIQUE(requester_id, addressee_id)
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_members (
    conv_id INTEGER NOT NULL REFERENCES conversations(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    UNIQUE(conv_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id INTEGER NOT NULL REFERENCES conversations(id),
    sender_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_reads (
    conv_id INTEGER NOT NULL REFERENCES conversations(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    last_read_at TEXT NOT NULL,
    UNIQUE(conv_id, user_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    ref TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
"""


class SocialStore:
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

    def _contact(self, row: sqlite3.Row | None) -> Contact | None:
        if row is None:
            return None
        return Contact(
            id=row["id"], requester_id=row["requester_id"],
            addressee_id=row["addressee_id"], status=row["status"],
            created=row["created"],
        )

    def _conversation(self, row: sqlite3.Row | None) -> Conversation | None:
        if row is None:
            return None
        return Conversation(
            id=row["id"], kind=row["kind"], title=row["title"], created=row["created"],
        )

    def _message(self, row: sqlite3.Row | None) -> Message | None:
        if row is None:
            return None
        return Message(
            id=row["id"], conv_id=row["conv_id"], sender_id=row["sender_id"],
            body=row["body"], created=row["created"],
        )

    def _notification(self, row: sqlite3.Row | None) -> Notification | None:
        if row is None:
            return None
        return Notification(
            id=row["id"], user_id=row["user_id"], kind=row["kind"], body=row["body"],
            ref=row["ref"], read=bool(row["read"]), created=row["created"],
        )

    # -- contacts (mutual consent — the anti-spam gate) -----------------------

    def request_contact(self, requester_id: int, addressee_id: int) -> Contact:
        if requester_id == addressee_id:
            raise SocialError("You cannot send a contact request to yourself.")
        with self._lock:
            if self._are_contacts_locked(requester_id, addressee_id):
                raise SocialError("You are already contacts.")
            # A pending request from the other direction? Both wanted it — auto-accept.
            reverse = self._conn.execute(
                "SELECT * FROM contacts WHERE requester_id = ? AND addressee_id = ? "
                "AND status = 'pending'",
                (addressee_id, requester_id),
            ).fetchone()
            if reverse is not None:
                self._conn.execute(
                    "UPDATE contacts SET status = 'accepted' WHERE id = ?", (reverse["id"],)
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM contacts WHERE id = ?", (reverse["id"],)
                ).fetchone()
                return self._contact(row)  # type: ignore[return-value]
            # A duplicate same-direction pending request is idempotent.
            existing = self._conn.execute(
                "SELECT * FROM contacts WHERE requester_id = ? AND addressee_id = ? "
                "AND status = 'pending'",
                (requester_id, addressee_id),
            ).fetchone()
            if existing is not None:
                return self._contact(existing)  # type: ignore[return-value]
            cur = self._conn.execute(
                "INSERT INTO contacts (requester_id, addressee_id, status, created) "
                "VALUES (?, ?, 'pending', ?)",
                (requester_id, addressee_id, _now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._contact(row)  # type: ignore[return-value]

    def accept_contact(self, user_id: int, requester_id: int) -> Contact:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE requester_id = ? AND addressee_id = ? "
                "AND status = 'pending'",
                (requester_id, user_id),
            ).fetchone()
            if row is None:
                raise SocialError(
                    f"No pending contact request from user {requester_id} to accept."
                )
            self._conn.execute(
                "UPDATE contacts SET status = 'accepted' WHERE id = ?", (row["id"],)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._contact(row)  # type: ignore[return-value]

    def _are_contacts_locked(self, a_id: int, b_id: int) -> bool:
        """Caller must hold self._lock."""
        row = self._conn.execute(
            "SELECT 1 FROM contacts WHERE status = 'accepted' AND "
            "((requester_id = ? AND addressee_id = ?) OR (requester_id = ? AND addressee_id = ?))",
            (a_id, b_id, b_id, a_id),
        ).fetchone()
        return row is not None

    def are_contacts(self, a_id: int, b_id: int) -> bool:
        with self._lock:
            return self._are_contacts_locked(a_id, b_id)

    def list_contacts(self, user_id: int) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contacts WHERE requester_id = ? OR addressee_id = ?",
                (user_id, user_id),
            ).fetchall()
        accepted: list[int] = []
        incoming: list[int] = []
        outgoing: list[int] = []
        for row in rows:
            other = row["addressee_id"] if row["requester_id"] == user_id else row["requester_id"]
            if row["status"] == "accepted":
                accepted.append(other)
            elif row["requester_id"] == user_id:
                outgoing.append(other)
            else:
                incoming.append(other)
        return {"accepted": accepted, "incoming": incoming, "outgoing": outgoing}

    # -- conversations + messages ----------------------------------------------

    def get_or_create_dm(self, a_id: int, b_id: int) -> Conversation:
        with self._lock:
            if not self._are_contacts_locked(a_id, b_id):
                raise SocialError(
                    "Not contacts — send a contact request first before starting a DM."
                )
            # Deterministic: exactly one dm conversation per unordered pair —
            # a dm conversation has exactly the two members, look it up first.
            rows = self._conn.execute(
                "SELECT c.id AS conv_id FROM conversations c "
                "JOIN conversation_members m1 ON m1.conv_id = c.id AND m1.user_id = ? "
                "JOIN conversation_members m2 ON m2.conv_id = c.id AND m2.user_id = ? "
                "WHERE c.kind = 'dm'",
                (a_id, b_id),
            ).fetchall()
            for row in rows:
                count = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM conversation_members WHERE conv_id = ?",
                    (row["conv_id"],),
                ).fetchone()["n"]
                if count == 2:
                    existing = self._conn.execute(
                        "SELECT * FROM conversations WHERE id = ?", (row["conv_id"],)
                    ).fetchone()
                    return self._conversation(existing)  # type: ignore[return-value]
            cur = self._conn.execute(
                "INSERT INTO conversations (kind, title, created) VALUES ('dm', NULL, ?)",
                (_now(),),
            )
            conv_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO conversation_members (conv_id, user_id) VALUES (?, ?), (?, ?)",
                (conv_id, a_id, conv_id, b_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            return self._conversation(row)  # type: ignore[return-value]

    def create_group(self, creator_id: int, title: str, member_ids: list[int]) -> Conversation:
        """Create a group conversation. No contact requirement in v1 — the
        creator vouches for whoever they add; unlike DMs this is not gated
        by mutual consent."""
        title = (title or "").strip()
        if not title:
            raise SocialError("A group conversation needs a title.")
        members = {creator_id, *member_ids}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO conversations (kind, title, created) VALUES ('group', ?, ?)",
                (title, _now()),
            )
            conv_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO conversation_members (conv_id, user_id) VALUES (?, ?)",
                [(conv_id, uid) for uid in members],
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            return self._conversation(row)  # type: ignore[return-value]

    def _is_member_locked(self, conv_id: int, user_id: int) -> bool:
        """Caller must hold self._lock."""
        row = self._conn.execute(
            "SELECT 1 FROM conversation_members WHERE conv_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        return row is not None

    def send_message(self, conv_id: int, sender_id: int, body: str) -> Message:
        """Store `body` verbatim. Secret-scanning is the CALLER's (tool-layer)
        responsibility, mirroring the brain's thread posts — this store does
        not inspect content, only membership and size."""
        if not body:
            raise SocialError("Message body cannot be empty.")
        if len(body) > _MAX_MESSAGE_LEN:
            raise SocialError(f"Message body exceeds {_MAX_MESSAGE_LEN} characters.")
        with self._lock:
            if not self._is_member_locked(conv_id, sender_id):
                raise SocialError(f"User {sender_id} is not a member of conversation {conv_id}.")
            cur = self._conn.execute(
                "INSERT INTO messages (conv_id, sender_id, body, created) VALUES (?, ?, ?, ?)",
                (conv_id, sender_id, body, _now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._message(row)  # type: ignore[return-value]

    def list_messages(
        self, conv_id: int, user_id: int, since: str | None = None, limit: int = 100
    ) -> list[Message]:
        with self._lock:
            if not self._is_member_locked(conv_id, user_id):
                raise SocialError(f"User {user_id} is not a member of conversation {conv_id}.")
            if since is not None:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE conv_id = ? AND created > ? "
                    "ORDER BY id ASC LIMIT ?",
                    (conv_id, since, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE conv_id = ? ORDER BY id ASC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
        return [self._message(r) for r in rows]  # type: ignore[misc]

    def mark_read(self, conv_id: int, user_id: int) -> None:
        with self._lock:
            if not self._is_member_locked(conv_id, user_id):
                raise SocialError(f"User {user_id} is not a member of conversation {conv_id}.")
            now = _now()
            self._conn.execute(
                "INSERT INTO conversation_reads (conv_id, user_id, last_read_at) VALUES (?, ?, ?) "
                "ON CONFLICT(conv_id, user_id) DO UPDATE SET last_read_at = excluded.last_read_at",
                (conv_id, user_id, now),
            )
            self._conn.commit()

    def list_conversations(self, user_id: int) -> list[dict]:
        with self._lock:
            conv_rows = self._conn.execute(
                "SELECT c.* FROM conversations c "
                "JOIN conversation_members m ON m.conv_id = c.id "
                "WHERE m.user_id = ? ORDER BY c.id",
                (user_id,),
            ).fetchall()
            results = []
            for conv_row in conv_rows:
                conv_id = conv_row["id"]
                member_rows = self._conn.execute(
                    "SELECT user_id FROM conversation_members WHERE conv_id = ?", (conv_id,)
                ).fetchall()
                members = [r["user_id"] for r in member_rows]
                last_row = self._conn.execute(
                    "SELECT * FROM messages WHERE conv_id = ? ORDER BY id DESC LIMIT 1", (conv_id,)
                ).fetchone()
                last_message = self._message(last_row)
                read_row = self._conn.execute(
                    "SELECT last_read_at FROM conversation_reads WHERE conv_id = ? AND user_id = ?",
                    (conv_id, user_id),
                ).fetchone()
                last_read_at = read_row["last_read_at"] if read_row is not None else ""
                unread = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE conv_id = ? AND created > ?",
                    (conv_id, last_read_at),
                ).fetchone()["n"]
                results.append({
                    "conv_id": conv_id,
                    "kind": conv_row["kind"],
                    "title": conv_row["title"],
                    "members": members,
                    "last_message": last_message,
                    "unread": unread,
                })
        return results

    # -- notifications ---------------------------------------------------------

    def create_notification(
        self, user_id: int, kind: str, body: str, ref: str | None = None
    ) -> Notification:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notifications (user_id, kind, body, ref, read, created) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (user_id, kind, body, ref, _now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        # Live delivery: wake any desktop long-poll parked on this user
        # (?wait= on /dashboard/api/notifications). Deliberately HERE — every
        # in-process writer funnels through this method, so no future caller
        # can forget the wake. Layering note: this store is sync; the wake is
        # scheduled onto the running loop when one exists (server), silently
        # skipped when none does (scripts, sync tests — their inserts land at
        # the end of the current wait cycle instead).
        try:
            import asyncio

            from engram_server import pushbus

            asyncio.get_running_loop().create_task(pushbus.wake(user_id))
        except RuntimeError:
            pass
        return self._notification(row)  # type: ignore[return-value]

    def list_notifications(
        self, user_id: int, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        with self._lock:
            if unread_only:
                rows = self._conn.execute(
                    "SELECT * FROM notifications WHERE user_id = ? AND read = 0 "
                    "ORDER BY id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        return [self._notification(r) for r in rows]  # type: ignore[misc]

    def mark_notifications_read(self, user_id: int, ids: list[int] | None = None) -> int:
        with self._lock:
            if ids is None:
                cur = self._conn.execute(
                    "UPDATE notifications SET read = 1 WHERE user_id = ? AND read = 0",
                    (user_id,),
                )
            else:
                if not ids:
                    return 0
                placeholders = ",".join("?" for _ in ids)
                cur = self._conn.execute(
                    f"UPDATE notifications SET read = 1 WHERE user_id = ? AND read = 0 "
                    f"AND id IN ({placeholders})",
                    (user_id, *ids),
                )
            self._conn.commit()
            return cur.rowcount

    def unread_counts(self, user_id: int) -> dict:
        with self._lock:
            conv_rows = self._conn.execute(
                "SELECT conv_id FROM conversation_members WHERE user_id = ?", (user_id,)
            ).fetchall()
            dms = 0
            for row in conv_rows:
                conv_id = row["conv_id"]
                read_row = self._conn.execute(
                    "SELECT last_read_at FROM conversation_reads WHERE conv_id = ? AND user_id = ?",
                    (conv_id, user_id),
                ).fetchone()
                last_read_at = read_row["last_read_at"] if read_row is not None else ""
                unread = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE conv_id = ? AND created > ?",
                    (conv_id, last_read_at),
                ).fetchone()["n"]
                dms += unread
            notif_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read = 0",
                (user_id,),
            ).fetchone()["n"]
        return {"dms": dms, "notifications": notif_count}
