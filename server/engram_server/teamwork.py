"""Teamwork store — rooms (live joins across brains) plus team presence.

Cross-user state never lives in a git brain, so rooms and presence are
sibling table sets in the SAME neutral SQLite file (engram.db) as
tenancy.py/social.py/discovery.py, referencing users(id) by plain INTEGER.

This module is MECHANISM only: integrity (membership, budgets, grant
boundaries) — not content policy. Secret-scanning turn/grant bodies is the
CALLER's (tool-layer) responsibility, mirroring social.send_message's and
discovery.create_ask's boundary notes.

A room exists to let two or more brains hold a live, budgeted conversation
toward a stated goal. `goal` and a turn budget are mandatory — the
"money-fire rule": an agent-to-agent conversation without an exit condition
and a hard stop will burn tokens forever. Once a room closes, any context
grants issued inside it are dead; a closed room cannot be revived, only
read.

Sync API by design, same as SocialStore/DiscoveryStore/TenancyStore:
microsecond-scale row operations guarded by one process-wide lock, safe to
call from async code without a threadpool.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import KBError

_MAX_TURN_LEN = 4000
# Above this a turn still posts, but the result carries a nudge toward `refs`.
# The soft limit is the real mechanism — the hard cap only catches runaways.
_LONG_TURN_CHARS = 1200
_MAX_PATH_LEN = 200
_MAX_HARD_CAP = 500
_MAX_EXTEND = 100

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

# Segments that are never shareable via a room grant — they're either
# cross-user ephemera (messages/inbox/workspace/threads) or plumbing (.git),
# never a legitimate slice of a project to hand another user.
_UNSHAREABLE_SEGMENTS = frozenset({"messages", "inbox", "workspace", "threads", ".git"})


class TeamworkError(KBError):
    """Room/presence misuse; the message teaches the caller the correct next step."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Room:
    id: int
    name: str
    creator_id: int
    goal: str
    exit_condition: str
    turn_budget: int
    hard_cap: int
    status: str  # open | closed
    outcome: str | None
    created: str
    closed_at: str | None


@dataclass(frozen=True)
class RoomTurn:
    id: int
    room_id: int
    user_id: int
    session: str
    kind: str  # message | system | guest_read
    body: str
    created: str
    # Concept paths attached to this turn — SHARE, don't paste. A room turn is a
    # claim plus a pointer; the document belongs in the brain where it can be
    # versioned, searched and read once instead of re-sent to every member.
    refs: tuple[str, ...] = ()


_ROOM_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    creator_id INTEGER NOT NULL REFERENCES users(id),
    goal TEXT NOT NULL,
    exit_condition TEXT NOT NULL DEFAULT '',
    turn_budget INTEGER NOT NULL DEFAULT 40,
    hard_cap INTEGER NOT NULL DEFAULT 200,
    status TEXT NOT NULL DEFAULT 'open',
    outcome TEXT,
    created TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS room_members (
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    invited_by INTEGER REFERENCES users(id),
    joined TEXT NOT NULL,
    PRIMARY KEY (room_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_room_members_user ON room_members(user_id);
CREATE TABLE IF NOT EXISTS room_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    session TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'message',
    body TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_turns_room_id ON room_turns(room_id, id);
CREATE TABLE IF NOT EXISTS room_reads (
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    last_turn_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room_id, user_id)
);
CREATE TABLE IF NOT EXISTS room_grants (
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    grantor_id INTEGER NOT NULL REFERENCES users(id),
    path_prefix TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (room_id, grantor_id, path_prefix)
);
"""


class RoomStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_ROOM_SCHEMA)
            # Turns predating `refs` (concept paths attached to a turn).
            tcols = {r[1] for r in self._conn.execute("PRAGMA table_info(room_turns)")}
            if "refs" not in tcols:
                self._conn.execute("ALTER TABLE room_turns ADD COLUMN refs TEXT")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- row -> dataclass helpers ---------------------------------------------

    def _room(self, row: sqlite3.Row | None) -> Room | None:
        if row is None:
            return None
        return Room(
            id=row["id"], name=row["name"], creator_id=row["creator_id"],
            goal=row["goal"], exit_condition=row["exit_condition"],
            turn_budget=row["turn_budget"], hard_cap=row["hard_cap"],
            status=row["status"], outcome=row["outcome"], created=row["created"],
            closed_at=row["closed_at"],
        )

    def _turn(self, row: sqlite3.Row | None) -> RoomTurn | None:
        if row is None:
            return None
        try:
            raw = row["refs"]
        except (IndexError, KeyError):
            raw = None
        refs: tuple[str, ...] = ()
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    refs = tuple(str(x) for x in loaded if str(x).strip())
            except (ValueError, TypeError):
                refs = ()
        return RoomTurn(
            id=row["id"], room_id=row["room_id"], user_id=row["user_id"],
            session=row["session"], kind=row["kind"], body=row["body"],
            created=row["created"], refs=refs,
        )

    # -- internal (caller must hold self._lock) --------------------------------

    def _room_locked(self, room_id: int) -> Room | None:
        row = self._conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return self._room(row)

    def _is_member_locked(self, room_id: int, user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        ).fetchone()
        return row is not None

    def _require_member_locked(self, room: Room, user_id: int) -> None:
        if not self._is_member_locked(room.id, user_id):
            raise TeamworkError(
                f"User {user_id} is not a member of room {room.name!r} — ask a member to invite() you."
            )

    def _write_turn_locked(
        self, room_id: int, user_id: int, body: str, *, kind: str = "system", session: str = "",
        refs: list[str] | None = None,
    ) -> RoomTurn:
        """Internal, ungated turn insert — used for system turns and by post_turn
        once its checks have already passed. Caller must hold self._lock."""
        clean = [r.strip() for r in (refs or []) if r and r.strip()][:20]
        cur = self._conn.execute(
            "INSERT INTO room_turns (room_id, user_id, session, kind, body, created, refs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (room_id, user_id, session, kind, body, _now(),
             json.dumps(clean) if clean else None),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM room_turns WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return self._turn(row)  # type: ignore[return-value]

    def _touch_read_locked(self, room_id: int, user_id: int, turn_id: int) -> None:
        self._conn.execute(
            "INSERT INTO room_reads (room_id, user_id, last_turn_id) VALUES (?, ?, ?) "
            "ON CONFLICT(room_id, user_id) DO UPDATE SET last_turn_id = "
            "MAX(last_turn_id, excluded.last_turn_id)",
            (room_id, user_id, turn_id),
        )

    # -- rooms ------------------------------------------------------------------

    def open_room(
        self,
        creator_id: int,
        name: str,
        goal: str,
        *,
        exit_condition: str = "",
        turn_budget: int = 40,
        hard_cap: int = 200,
    ) -> Room:
        slug = name.strip().lower()
        if not _SLUG_RE.match(slug):
            raise TeamworkError(
                f"Room name {name!r} is invalid: lowercase letters, digits and hyphens only, "
                "3-64 characters, starting with a letter or digit."
            )
        goal = (goal or "").strip()
        if not goal:
            raise TeamworkError(
                "A room needs a goal — rooms are for agent-to-agent conversations that must "
                "terminate (the money-fire rule); state what success looks like, and set an "
                "exit_condition too if you can."
            )
        if not (1 <= turn_budget <= hard_cap <= _MAX_HARD_CAP):
            raise TeamworkError(
                f"Budgets must satisfy 1 <= turn_budget <= hard_cap <= {_MAX_HARD_CAP} "
                f"(got turn_budget={turn_budget}, hard_cap={hard_cap})."
            )
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM rooms WHERE name = ?", (slug,)
            ).fetchone()
            if existing is not None:
                raise TeamworkError(
                    f"Room name {slug!r} is already taken — try a suffix like {slug}-2."
                )
            now = _now()
            cur = self._conn.execute(
                "INSERT INTO rooms (name, creator_id, goal, exit_condition, turn_budget, "
                "hard_cap, status, outcome, created, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?, NULL)",
                (slug, creator_id, goal, exit_condition, turn_budget, hard_cap, now),
            )
            room_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO room_members (room_id, user_id, invited_by, joined) "
                "VALUES (?, ?, NULL, ?)",
                (room_id, creator_id, now),
            )
            self._conn.commit()
            self._write_turn_locked(room_id, creator_id, f"room opened: {goal}", kind="system")
            row = self._conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            return self._room(row)  # type: ignore[return-value]

    def room(self, room_id: int) -> Room | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return self._room(row)

    def room_by_name(self, name: str) -> Room | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rooms WHERE name = ?", (name.strip().lower(),)
            ).fetchone()
        return self._room(row)

    def invite(self, room_id: int, inviter_id: int, invitee_id: int) -> bool:
        if inviter_id == invitee_id:
            return False
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            self._require_member_locked(room, inviter_id)
            if room.status != "open":
                raise TeamworkError(f"Room {room.name!r} is closed — cannot invite new members.")
            if self._is_member_locked(room_id, invitee_id):
                return False
            self._conn.execute(
                "INSERT INTO room_members (room_id, user_id, invited_by, joined) "
                "VALUES (?, ?, ?, ?)",
                (room_id, invitee_id, inviter_id, _now()),
            )
            self._conn.commit()
            return True

    def members(self, room_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, invited_by, joined FROM room_members "
                "WHERE room_id = ? ORDER BY joined",
                (room_id,),
            ).fetchall()
        return [
            {"user_id": r["user_id"], "invited_by": r["invited_by"], "joined": r["joined"]}
            for r in rows
        ]

    def post_turn(
        self, room_id: int, user_id: int, body: str, *, session: str = "",
        kind: str = "message", refs: list[str] | None = None,
    ) -> RoomTurn:
        body = (body or "").strip()
        if not body:
            raise TeamworkError("Turn body cannot be empty.")
        if len(body) > _MAX_TURN_LEN:
            # The cap is a backstop, not the lesson. Refusing with "too long"
            # teaches splitting into two long turns (observed); refusing with the
            # right move teaches the shape we actually want.
            raise TeamworkError(
                f"Turn body is {len(body)} chars, over the {_MAX_TURN_LEN} cap. Don't "
                "split it into two long turns — that costs every member twice. Write it "
                "as a concept (kb_write) and post a one-line summary with its path in "
                "`refs`: shared that way it's versioned, searchable and read on demand."
            )
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            self._require_member_locked(room, user_id)
            if room.status != "open":
                raise TeamworkError(f"Room {room.name!r} is closed — no further turns can be posted.")
            if kind == "message":
                count = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM room_turns WHERE room_id = ? AND kind = 'message'",
                    (room_id,),
                ).fetchone()["n"]
                if count >= room.hard_cap:
                    raise TeamworkError(
                        f"Room {room.name!r} hit its hard cap ({room.hard_cap} messages) — "
                        "close it with an outcome instead of posting further."
                    )
                if count >= room.turn_budget:
                    raise TeamworkError(
                        f"Room {room.name!r} hit its turn budget ({room.turn_budget} messages) — "
                        f"goal: {room.goal!r}"
                        + (f", exit_condition: {room.exit_condition!r}" if room.exit_condition else "")
                        + ". Call extend_budget() if the conversation genuinely needs more room, "
                        "or wrap up and close_room() with an outcome."
                    )
            turn = self._write_turn_locked(room_id, user_id, body, kind=kind, session=session, refs=refs)
            self._touch_read_locked(room_id, user_id, turn.id)
            self._conn.commit()
            return turn

    def extend_budget(self, room_id: int, user_id: int, extra: int) -> Room:
        if not (1 <= extra <= _MAX_EXTEND):
            raise TeamworkError(f"extra must be between 1 and {_MAX_EXTEND} (got {extra}).")
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            self._require_member_locked(room, user_id)
            if room.status != "open":
                raise TeamworkError(f"Room {room.name!r} is closed — nothing to extend.")
            new_budget = min(room.turn_budget + extra, room.hard_cap)
            self._conn.execute(
                "UPDATE rooms SET turn_budget = ? WHERE id = ?", (new_budget, room_id)
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            return self._room(row)  # type: ignore[return-value]

    def read_turns(
        self, room_id: int, user_id: int, since_id: int = 0, limit: int = 200
    ) -> list[RoomTurn]:
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            self._require_member_locked(room, user_id)
            rows = self._conn.execute(
                "SELECT * FROM room_turns WHERE room_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (room_id, since_id, limit),
            ).fetchall()
            turns = [self._turn(r) for r in rows]  # type: ignore[misc]
            if turns:
                self._touch_read_locked(room_id, user_id, turns[-1].id)
                self._conn.commit()
        return turns

    def last_turn_id(self, room_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) AS m FROM room_turns WHERE room_id = ?", (room_id,)
            ).fetchone()
        return row["m"] or 0

    def list_rooms_for(self, user_id: int, include_closed: bool = False) -> list[dict]:
        with self._lock:
            if include_closed:
                rows = self._conn.execute(
                    "SELECT r.* FROM rooms r JOIN room_members m ON m.room_id = r.id "
                    "WHERE m.user_id = ? ORDER BY r.id",
                    (user_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT r.* FROM rooms r JOIN room_members m ON m.room_id = r.id "
                    "WHERE m.user_id = ? AND r.status = 'open' ORDER BY r.id",
                    (user_id,),
                ).fetchall()
            results = []
            for row in rows:
                room = self._room(row)
                assert room is not None
                member_rows = self._conn.execute(
                    "SELECT user_id FROM room_members WHERE room_id = ?", (room.id,)
                ).fetchall()
                member_ids = [r["user_id"] for r in member_rows]
                last_id = self._conn.execute(
                    "SELECT MAX(id) AS m FROM room_turns WHERE room_id = ?", (room.id,)
                ).fetchone()["m"] or 0
                read_row = self._conn.execute(
                    "SELECT last_turn_id FROM room_reads WHERE room_id = ? AND user_id = ?",
                    (room.id, user_id),
                ).fetchone()
                read_floor = read_row["last_turn_id"] if read_row is not None else 0
                unread = max(0, last_id - read_floor)
                messages_used = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM room_turns WHERE room_id = ? AND kind = 'message'",
                    (room.id,),
                ).fetchone()["n"]
                last_turn_row = self._conn.execute(
                    "SELECT user_id, kind, body, created FROM room_turns "
                    "WHERE room_id = ? ORDER BY id DESC LIMIT 1",
                    (room.id,),
                ).fetchone()
                last_turn = None
                if last_turn_row is not None:
                    last_turn = {
                        "author_id": last_turn_row["user_id"],
                        "kind": last_turn_row["kind"],
                        "body": last_turn_row["body"][:200],
                        "created": last_turn_row["created"],
                    }
                results.append({
                    "id": room.id,
                    "name": room.name,
                    "creator_id": room.creator_id,
                    "goal": room.goal,
                    "exit_condition": room.exit_condition,
                    "turn_budget": room.turn_budget,
                    "hard_cap": room.hard_cap,
                    "status": room.status,
                    "outcome": room.outcome,
                    "created": room.created,
                    "closed_at": room.closed_at,
                    "member_count": len(member_ids),
                    "member_ids": member_ids,
                    "last_turn_id": last_id,
                    "unread": unread,
                    "messages_used": messages_used,
                    "last_turn": last_turn,
                })
        return results

    def close_room(self, room_id: int, user_id: int, outcome: str = "") -> Room:
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            self._require_member_locked(room, user_id)
            if room.status == "closed":
                raise TeamworkError(f"Room {room.name!r} is already closed.")
            now = _now()
            self._conn.execute(
                "UPDATE rooms SET status = 'closed', outcome = ?, closed_at = ? WHERE id = ?",
                (outcome, now, room_id),
            )
            self._conn.commit()
            self._write_turn_locked(room_id, user_id, "room closed", kind="system")
            row = self._conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            return self._room(row)  # type: ignore[return-value]

    # -- grants (context shared inside a room; dead once the room closes) -----

    @staticmethod
    def _normalize_path_prefix(path_prefix: str) -> str:
        raw = (path_prefix or "").strip()
        if not raw:
            raise TeamworkError("path_prefix cannot be empty.")
        if len(raw) > _MAX_PATH_LEN:
            raise TeamworkError(f"path_prefix exceeds {_MAX_PATH_LEN} characters.")
        normalized = raw.replace("\\", "/").strip()
        if normalized.startswith("/"):
            raise TeamworkError(f"path_prefix {path_prefix!r} must be relative — no leading '/'.")
        while normalized.endswith("/"):
            normalized = normalized[:-1]
        if not normalized:
            raise TeamworkError("path_prefix cannot be empty.")
        segments = normalized.split("/")
        if ".." in segments:
            raise TeamworkError(f"path_prefix {path_prefix!r} cannot contain '..'.")
        bad = _UNSHAREABLE_SEGMENTS.intersection(segments)
        if bad:
            raise TeamworkError(
                f"path_prefix {path_prefix!r} touches {sorted(bad)!r} — those segments are "
                "never shareable via a room grant."
            )
        return normalized

    def add_grant(self, room_id: int, grantor_id: int, path_prefix: str) -> None:
        normalized = self._normalize_path_prefix(path_prefix)
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            self._require_member_locked(room, grantor_id)
            if room.status != "open":
                raise TeamworkError(f"Room {room.name!r} is closed — cannot add a grant.")
            self._conn.execute(
                "INSERT INTO room_grants (room_id, grantor_id, path_prefix, created) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(room_id, grantor_id, path_prefix) DO NOTHING",
                (room_id, grantor_id, normalized, _now()),
            )
            self._conn.commit()

    def revoke_grant(self, room_id: int, grantor_id: int, path_prefix: str) -> None:
        normalized = self._normalize_path_prefix(path_prefix)
        with self._lock:
            self._conn.execute(
                "DELETE FROM room_grants WHERE room_id = ? AND grantor_id = ? AND path_prefix = ?",
                (room_id, grantor_id, normalized),
            )
            self._conn.commit()

    def grants_for(self, room_id: int) -> list[dict]:
        with self._lock:
            room = self._room_locked(room_id)
            if room is None or room.status == "closed":
                return []
            rows = self._conn.execute(
                "SELECT grantor_id, path_prefix, created FROM room_grants "
                "WHERE room_id = ? ORDER BY created",
                (room_id,),
            ).fetchall()
        return [
            {"grantor_id": r["grantor_id"], "path_prefix": r["path_prefix"], "created": r["created"]}
            for r in rows
        ]

    def assert_grant(self, room_id: int, grantor_id: int, path: str) -> None:
        check_path = (path or "").strip().replace("\\", "/")
        while check_path.endswith("/"):
            check_path = check_path[:-1]
        # The boundary must be SELF-SUFFICIENT (sec-review): a '../' path is a
        # textual prefix-match of the grant while resolving elsewhere. KBStore
        # rejects such paths independently today, but any other consumer of this
        # check (a cache, the semantic index) must be equally safe on its own.
        segments = [seg for seg in check_path.split("/") if seg]
        if any(seg in (".", "..") for seg in segments) or check_path.startswith("/"):
            raise TeamworkError(
                f"Path {path!r} contains '.'/'..' segments or is absolute — "
                "grant checks require a plain repo-relative path."
            )
        with self._lock:
            room = self._room_locked(room_id)
            if room is None or room.status != "open":
                raise TeamworkError(
                    f"Room {room_id} is closed or missing — grants inside it are dead."
                )
            rows = self._conn.execute(
                "SELECT path_prefix FROM room_grants WHERE room_id = ? AND grantor_id = ?",
                (room_id, grantor_id),
            ).fetchall()
        for row in rows:
            prefix = row["path_prefix"]
            if check_path == prefix or check_path.startswith(prefix + "/"):
                return
        raise TeamworkError(
            f"User {grantor_id} has not granted access to {path!r} in room {room_id}."
        )


_PRESENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_presence (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    project TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    invisible INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL
);
"""


class PresenceStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_PRESENCE_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def touch(self, user_id: int, *, tool: str = "", project: str | None = None) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT project FROM team_presence WHERE user_id = ?", (user_id,)
            ).fetchone()
            if project is None:
                new_project = existing["project"] if existing is not None else ""
            else:
                new_project = project
            self._conn.execute(
                "INSERT INTO team_presence (user_id, project, tool, invisible, updated) "
                "VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET project = excluded.project, "
                "tool = excluded.tool, updated = excluded.updated",
                (user_id, new_project, tool, _now()),
            )
            self._conn.commit()

    def set_project(self, user_id: int, project: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO team_presence (user_id, project, tool, invisible, updated) "
                "VALUES (?, ?, '', 0, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET project = excluded.project, "
                "updated = excluded.updated",
                (user_id, project, _now()),
            )
            self._conn.commit()

    def set_invisible(self, user_id: int, on: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO team_presence (user_id, project, tool, invisible, updated) "
                "VALUES (?, '', '', ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET invisible = excluded.invisible, "
                "updated = excluded.updated",
                (user_id, int(bool(on)), _now()),
            )
            self._conn.commit()

    def roster(self, active_minutes: int = 120) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM team_presence WHERE invisible = 0 ORDER BY updated DESC"
            ).fetchall()
        now = dt.datetime.now(dt.timezone.utc)
        results = []
        for row in rows:
            updated = dt.datetime.strptime(row["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc
            )
            minutes_ago = int((now - updated).total_seconds() // 60)
            if minutes_ago > active_minutes:
                continue
            results.append({
                "user_id": row["user_id"],
                "project": row["project"],
                "tool": row["tool"],
                "invisible": bool(row["invisible"]),
                "updated": row["updated"],
                "minutes_ago": minutes_ago,
            })
        return results

    def self_row(self, user_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_presence WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        updated = dt.datetime.strptime(row["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
        minutes_ago = int((now - updated).total_seconds() // 60)
        return {
            "user_id": row["user_id"],
            "project": row["project"],
            "tool": row["tool"],
            "invisible": bool(row["invisible"]),
            "updated": row["updated"],
            "minutes_ago": minutes_ago,
        }


# ---------------------------------------------------------------- long-poll bus
# The waiting primitive shared by EVERY room writer (MCP tools in app.py, the web
# reply form in dashboard.py): posting a turn calls room_notify(room_id); a waiting
# reader calls room_wait(room_id, seconds). asyncio-level (the stores stay sync) and
# in-process — one server, one loop, so a Condition per room is sufficient and a
# waiting agent costs nothing while idle. Never add client-side polling on top.

import asyncio as _asyncio

_room_conditions: dict[int, _asyncio.Condition] = {}


def _room_condition(room_id: int) -> _asyncio.Condition:
    cond = _room_conditions.get(room_id)
    if cond is None:
        cond = _room_conditions[room_id] = _asyncio.Condition()
    return cond


async def room_notify(room_id: int) -> None:
    """Wake every waiter on a room — call after ANY turn lands (message/system/audit)."""
    cond = _room_condition(room_id)
    async with cond:
        cond.notify_all()


async def room_wait(room_id: int, seconds: int) -> None:
    """Block until the room sees a new turn or the timeout lapses (1..120s clamp)."""
    cond = _room_condition(room_id)
    try:
        async with cond:
            await _asyncio.wait_for(cond.wait(), timeout=max(1, min(120, seconds)))
    except (TimeoutError, _asyncio.TimeoutError):
        pass
