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
from typing import Any

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


# The floor holder when a room is blocked on the PERSON. Not a speaker key: no
# session can ever be assigned it, which is the point — every agent sees "not me,
# and not any of us".
HUMAN_FLOOR = "human"

# How long since we last heard from a session before we stop routing turns to it.
# Generous, because being wrongly declared dead is worse than a slow rotation.
_SPEAKER_LIVE_MINUTES = 30


def _is_fresh(stamp: str, minutes: int = _SPEAKER_LIVE_MINUTES) -> bool:
    """True if an ISO-8601 stamp is within `minutes` of now. Unparseable = not
    fresh: an unreadable timestamp must never make a dead session look alive."""
    if not stamp:
        return False
    try:
        seen = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return False
    return (dt.datetime.now(dt.timezone.utc) - seen) <= dt.timedelta(minutes=minutes)


def _is_human_session(session: str) -> bool:
    """Did a PERSON write this turn, or their agent? The web composer and the
    dashboard reply form are people; `claude`/`claude:<key>` is an agent.

    `relay:<key>` is a person too — their words, typed to their own Claude in
    whatever conversation they were already in, and passed through. It counts as
    human because the content and the decision are theirs; it is kept DISTINCT
    from `human` so the transcript never claims they were in the room when they
    were not."""
    s = (session or "").strip()
    return (
        s in ("web", "app", "human")
        or s.startswith("dashboard:")
        or s.startswith("relay:")
    )


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
    # Floor control: the speaker key whose turn it is to talk. '' = open floor
    # (anyone may speak). See RoomStore.floor_state for what this is FOR.
    floor: str = ""
    floor_since: str | None = None
    # The question a session escalated to the person. Non-empty = the room is
    # blocked on a HUMAN, not on another agent.
    awaiting_human: str = ""
    awaiting_human_for: str = ""


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
CREATE TABLE IF NOT EXISTS room_speakers (
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    speaker TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    listening_until TEXT NOT NULL DEFAULT '',
    last_spoke TEXT NOT NULL DEFAULT '',
    empty_waits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room_id, speaker)
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
            # Rooms predating floor control (whose turn it is to speak).
            rcols = {r[1] for r in self._conn.execute("PRAGMA table_info(rooms)")}
            if "floor" not in rcols:
                self._conn.execute("ALTER TABLE rooms ADD COLUMN floor TEXT NOT NULL DEFAULT ''")
            if "floor_since" not in rcols:
                self._conn.execute("ALTER TABLE rooms ADD COLUMN floor_since TEXT")
            if "awaiting_human" not in rcols:
                self._conn.execute(
                    "ALTER TABLE rooms ADD COLUMN awaiting_human TEXT NOT NULL DEFAULT ''"
                )
            if "awaiting_human_for" not in rcols:
                self._conn.execute(
                    "ALTER TABLE rooms ADD COLUMN awaiting_human_for TEXT NOT NULL DEFAULT ''"
                )
            scols = {r[1] for r in self._conn.execute("PRAGMA table_info(room_speakers)")}
            if scols and "last_spoke" not in scols:
                self._conn.execute(
                    "ALTER TABLE room_speakers ADD COLUMN last_spoke TEXT NOT NULL DEFAULT ''"
                )
            if scols and "empty_waits" not in scols:
                self._conn.execute(
                    "ALTER TABLE room_speakers ADD COLUMN empty_waits INTEGER NOT NULL DEFAULT 0"
                )
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
            floor=(self._col(row, "floor") or ""),
            floor_since=self._col(row, "floor_since"),
            awaiting_human=(self._col(row, "awaiting_human") or ""),
            awaiting_human_for=(self._col(row, "awaiting_human_for") or ""),
        )

    @staticmethod
    def _col(row: sqlite3.Row, name: str) -> Any:
        """Read a column that may not exist on rows from an older schema."""
        try:
            return row[name]
        except (IndexError, KeyError):
            return None

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

    # -- floor control ----------------------------------------------------------
    #
    # THE PROBLEM THIS SOLVES. Two agent sessions in a room have no way to tell
    # "they are composing a reply" from "they went away". Both failure modes were
    # observed in one afternoon: two sessions each waiting for the other and
    # nobody speaking, and a session parked on a reply that was never coming
    # because the other side had moved on. Politeness deadlocks; impatience talks
    # over. Neither is fixable by asking agents to be more careful — the
    # information genuinely isn't there.
    #
    # So the room carries it explicitly. `floor` is the speaker key whose turn it
    # is; posting hands it to the other party; '' means open (anyone may speak).
    # A speaker that is long-polling is recorded as LISTENING with an expiry, so
    # the talker can see someone is actually there. The floor is advisory —
    # posting out of turn always succeeds, it just tells you what you did. Same
    # nudge pattern as the rest of the system: never block the human's intent.

    def touch_speaker(
        self, room_id: int, speaker: str, user_id: int, *, name: str = "",
        listening_seconds: int = 0,
    ) -> None:
        """Register/refresh a speaker in a room. `listening_seconds` > 0 marks them
        as actively listening until that many seconds from now."""
        speaker = (speaker or "").strip()
        if not speaker:
            return
        now = _now()
        name = name.strip()
        until = ""
        if listening_seconds > 0:
            until = (
                dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(seconds=min(600, listening_seconds))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._conn.execute(
                "INSERT INTO room_speakers (room_id, speaker, user_id, name, first_seen, "
                "last_seen, listening_until) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(room_id, speaker) DO UPDATE SET last_seen = excluded.last_seen, "
                "name = CASE WHEN excluded.name != '' THEN excluded.name ELSE room_speakers.name END, "
                "listening_until = CASE WHEN excluded.listening_until != '' "
                "THEN excluded.listening_until ELSE room_speakers.listening_until END",
                (room_id, speaker, user_id, name, now, now, until),
            )
            if name:
                self._absorb_duplicates_locked(room_id, speaker, name)
            self._claim_open_floor_locked(room_id, speaker)
            self._conn.commit()

    def _absorb_duplicates_locked(self, room_id: int, speaker: str, name: str) -> None:
        """Fold older rows carrying the same NAME into this one.

        A reconnect gives a session a brand-new key — the server restarting is
        enough — so one session accumulates a row per connection. Left alone,
        rotation hands the floor to keys nobody holds any more, and the room
        stalls behind a speaker that cannot answer while looking perfectly alive
        (its last_seen is recent). Observed live: six speakers for three
        sessions after two restarts.

        The name an agent gives itself survives reconnects, so it is the real
        identity; the key is only a fallback for sessions that never named
        themselves. Re-announcing absorbs the ghosts, carrying forward the
        speaking history so rotation stays fair, and rescues the floor if a
        ghost was holding it."""
        rows = self._conn.execute(
            "SELECT speaker, last_spoke FROM room_speakers "
            "WHERE room_id = ? AND name = ? AND speaker != ?",
            (room_id, name, speaker),
        ).fetchall()
        if not rows:
            return
        newest = max((str(r["last_spoke"] or "") for r in rows), default="")
        current = self._conn.execute(
            "SELECT last_spoke FROM room_speakers WHERE room_id = ? AND speaker = ?",
            (room_id, speaker),
        ).fetchone()
        if newest > str((current["last_spoke"] if current else "") or ""):
            self._conn.execute(
                "UPDATE room_speakers SET last_spoke = ? WHERE room_id = ? AND speaker = ?",
                (newest, room_id, speaker),
            )
        room = self._room_locked(room_id)
        held = {str(r["speaker"]) for r in rows}
        self._conn.executemany(
            "DELETE FROM room_speakers WHERE room_id = ? AND speaker = ?",
            [(room_id, str(r["speaker"])) for r in rows],
        )
        if room is not None and room.floor in held:
            # The floor belonged to a previous incarnation of this same session.
            self._set_floor_locked(room_id, speaker)
        if room is not None and room.awaiting_human_for in held:
            # So did the pending question. Miss this and the human's answer is
            # handed to a speaker that no longer exists: the floor is held by
            # nobody, is_you is false for everyone, and the room stays frozen
            # exactly as if still blocked — with no way for any agent to claim
            # it. Caught before it fired, on a room waiting on Hiren.
            self._conn.execute(
                "UPDATE rooms SET awaiting_human_for = ? WHERE id = ?",
                (speaker, room_id),
            )

    def _claim_open_floor_locked(self, room_id: int, speaker: str) -> None:
        """An arriving session picks up an OPEN floor if it owes the room a reply.

        Without this, whoever speaks first in a new room speaks to an empty house —
        there is nobody registered yet, so the floor opens, and when the second
        session finally reads the room it sees 'open' and no obligation. Both then
        wait. Resolving it on arrival closes that window: the newcomer is told, in
        the same call it used to read, that the turn is theirs.

        Only when someone has spoken more recently than you have — so a session
        re-reading a room it just posted in never takes its own floor back."""
        room = self._room_locked(room_id)
        if room is None or room.floor or room.awaiting_human or room.status != "open":
            return
        rows = self._speakers_locked(room_id)
        mine = next((s for s in rows if s["speaker"] == speaker), None)
        if mine is None:
            return
        newest_other = max(
            (str(s["last_spoke"]) for s in rows if s["speaker"] != speaker), default=""
        )
        if newest_other and newest_other > str(mine["last_spoke"]):
            self._set_floor_locked(room_id, speaker)

    # After this many polls in a row that returned nothing, stop waiting and hand
    # the turn back to the user. Straight from the PARK pattern playbook, where a
    # model rests after ~4 timeouts rather than looping forever. Without a count,
    # an agent has no way to tell its first empty wait from its twentieth, and
    # every one of them costs the user tokens for silence.
    REST_AFTER_EMPTY_WAITS = 4

    def note_empty_wait(self, room_id: int, speaker: str) -> int:
        """Record a long-poll that returned nothing; returns the running count."""
        if not speaker:
            return 0
        with self._lock:
            self._conn.execute(
                "UPDATE room_speakers SET empty_waits = empty_waits + 1 "
                "WHERE room_id = ? AND speaker = ?",
                (room_id, speaker),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT empty_waits FROM room_speakers WHERE room_id = ? AND speaker = ?",
                (room_id, speaker),
            ).fetchone()
        return int(row["empty_waits"]) if row else 0

    def clear_empty_waits(self, room_id: int, speaker: str) -> None:
        """Something arrived, or you spoke — the drought is over."""
        if not speaker:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE room_speakers SET empty_waits = 0 WHERE room_id = ? AND speaker = ?",
                (room_id, speaker),
            )
            self._conn.commit()

    def stop_listening(self, room_id: int, speaker: str) -> None:
        """End a listen the moment the poll is satisfied.

        The window is stamped at poll ENTRY and sized to outlast the poll, so a
        session looping its long-poll stays continuously 'listening' across the
        1-3s gap between calls. But a poll that returns EARLY — because someone
        spoke — leaves the rest of that window advertising a listener who has
        gone off to act on what they read. The other side then waits on a reply
        that nobody is composing, which is the precise false positive this signal
        exists to eliminate."""
        if not speaker:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE room_speakers SET listening_until = '' WHERE room_id = ? AND speaker = ?",
                (room_id, speaker),
            )
            self._conn.commit()

    def _speakers_locked(self, room_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM room_speakers WHERE room_id = ? ORDER BY first_seen",
            (room_id,),
        ).fetchall()
        now = _now()
        out: list[dict[str, Any]] = []
        for r in rows:
            last_seen = r["last_seen"]
            out.append({
                "speaker": r["speaker"],
                "name": r["name"] or r["speaker"],
                "user_id": r["user_id"],
                "last_seen": last_seen,
                "last_spoke": (self._col(r, "last_spoke") or ""),
                "empty_waits": int(self._col(r, "empty_waits") or 0),
                "live": _is_fresh(last_seen),
                # A listening window that has expired is not listening. This is
                # deliberately a timestamp rather than a boolean flag: a session
                # that dies mid-poll can't clear a flag, and a stuck "listening"
                # would be worse than none at all.
                "listening": bool(r["listening_until"]) and r["listening_until"] > now,
            })
        return out

    def speakers(self, room_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return self._speakers_locked(room_id)

    def _set_floor_locked(self, room_id: int, speaker: str) -> None:
        self._conn.execute(
            "UPDATE rooms SET floor = ?, floor_since = ? WHERE id = ?",
            (speaker or "", _now(), room_id),
        )

    def _resolve_speaker_locked(self, room_id: int, wanted: str) -> str:
        """Map a hand_to value onto a known speaker key. Accepts either the key or
        the friendly name, so an agent can hand the floor to 'mac' without knowing
        the opaque session key."""
        wanted = (wanted or "").strip()
        if not wanted:
            return ""
        for s in self._speakers_locked(room_id):
            if wanted == s["speaker"] or wanted.lower() == str(s["name"]).lower():
                return str(s["speaker"])
        return wanted

    def _pass_floor_locked(self, room_id: int, me: str, hand_to: str | None) -> None:
        """Posting hands the floor on.

        Explicit `hand_to` always wins. Otherwise the floor rotates to the LIVE
        speaker who has gone longest without speaking (never-spoken first, so a
        session that just joined gets drawn in rather than lurking). With two
        parties that degenerates to the obvious 'the other one', and with three or
        more it is a fair round-robin — which matters because an open floor among
        three agents reproduces the exact deadlock this mechanism exists to
        prevent: everyone sees 'not mine' and nobody speaks.

        Dead speakers are skipped. Handing the floor to a session that went home
        parks the whole room behind someone who will never answer, so if nobody is
        live the floor opens and `stalled` says why."""
        room = self._room_locked(room_id)
        if room is not None and room.awaiting_human:
            # The room is blocked on a person. An agent posting while it waits is
            # commentary, not an answer — it must not quietly cancel the question
            # another session asked, or the human is never told and everyone waits.
            return
        if hand_to is not None:
            self._set_floor_locked(room_id, self._resolve_speaker_locked(room_id, hand_to))
            return
        others = [s for s in self._speakers_locked(room_id) if s["speaker"] != me]
        live = [s for s in others if s["live"] or s["listening"]]
        if not live:
            self._set_floor_locked(room_id, "")
            return
        nxt = min(live, key=lambda s: (str(s["last_spoke"]), str(s["speaker"])))
        self._set_floor_locked(room_id, str(nxt["speaker"]))

    def record_speech(
        self, room_id: int, speaker: str, hand_to: str | None = None
    ) -> None:
        """Register that `speaker` just spoke, and pass the floor on — without
        writing a turn.

        Threads keep their transcript in git; only the coordination state lives
        here. Faking a turn to move the floor would put a phantom message in a
        transcript, and would burn the turn budget of a room nobody reads."""
        if not speaker:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE room_speakers SET listening_until = '', last_spoke = ? "
                "WHERE room_id = ? AND speaker = ?",
                (_now(), room_id, speaker),
            )
            self._pass_floor_locked(room_id, speaker, hand_to)
            self._conn.commit()

    def ask_human(self, room_id: int, question: str, asker: str = "") -> None:
        """Mark the room as blocked on the PERSON, not on another agent.

        Without this, a session that needs a human decision looks identical to one
        that is thinking, so every other session keeps waiting and the human — the
        only one who can unblock it — is never told. The floor moves to the human
        sentinel: no agent holds it, all of them can see why, and the question
        travels with it."""
        with self._lock:
            self._conn.execute(
                "UPDATE rooms SET awaiting_human = ?, awaiting_human_for = ? WHERE id = ?",
                (question.strip()[:500], asker or "", room_id),
            )
            self._set_floor_locked(room_id, HUMAN_FLOOR)
            self._conn.commit()

    def answer_human(self, room_id: int) -> None:
        """Clear a human block explicitly, for surfaces whose transcript is not in
        the room tables. A thread's turns live in git, so nothing passes through
        post_turn to notice the answer — without this, ask_human on a thread could
        block it forever."""
        with self._lock:
            self._human_answered_locked(room_id)
            self._conn.commit()

    def _human_answered_locked(self, room_id: int) -> None:
        """A human turn clears the block and hands the floor back to whoever asked."""
        room = self._room_locked(room_id)
        if room is None or not room.awaiting_human:
            return
        self._conn.execute(
            "UPDATE rooms SET awaiting_human = '', awaiting_human_for = '' WHERE id = ?",
            (room_id,),
        )
        # Hand the answer back to whoever asked — but only if they still exist.
        # An asker that has gone (or was absorbed before we could rewrite it)
        # would leave the floor held by nobody: is_you false for everyone, no way
        # to claim it, and a room that stays frozen exactly as if still blocked.
        # Opening the floor is always recoverable; a phantom holder is not.
        asker = room.awaiting_human_for or ""
        if asker and not any(
            s["speaker"] == asker for s in self._speakers_locked(room_id)
        ):
            asker = ""
        self._set_floor_locked(room_id, asker)

    def floor_state(self, room_id: int, me: str = "") -> dict[str, Any]:
        """Who holds the floor, who else is here, and whether anyone is listening.

        `alone` is the one every agent must check before waiting: a room with no
        other speaker cannot reply, however long you poll. Reporting that plainly
        is the difference between 'they're thinking' and 'nobody is coming'."""
        with self._lock:
            room = self._room_locked(room_id)
            if room is None:
                raise TeamworkError(f"No room with id {room_id}.")
            speakers = self._speakers_locked(room_id)
        others = [s for s in speakers if s["speaker"] != me]
        holder = room.floor
        held_by = next((s for s in speakers if s["speaker"] == holder), None)
        live_others = [s for s in others if s["live"] or s["listening"]]
        holder_name = held_by["name"] if held_by else holder
        if holder == HUMAN_FLOOR:
            holder_name = "the human"
        # ONE sentence saying what to do, because five booleans is four too many.
        # Every session in the live test read the flags correctly and still had to
        # reason its way to an action; the reasoning is identical every time, so
        # the server should just do it. The flags stay for anyone who wants them.
        me_holds = bool(holder) and holder == me
        mine = next((s for s in speakers if s["speaker"] == me), None)
        waited = int(mine["empty_waits"]) if mine else 0
        # Don't tell a correctly-parked session to give up on someone who is
        # visibly still there. Four empty polls means "nobody is coming" only if
        # nobody shows signs of coming; if the floor-holder is listening, or was
        # seen moments ago, they are composing and waiting is right. Raised by
        # mac-b, who spotted that the counter alone cannot tell a long compose
        # from an abandoned room.
        holder_active = held_by is not None and (
            held_by["listening"] or _is_fresh(str(held_by["last_seen"]), minutes=2)
        )
        # THE FLOOR-HOLDER IS THE CONVERSATION. If they are gone, nothing can
        # advance no matter how many others are alive — and `stalled` used to be
        # computed from the others alone, so a dead holder with one live bystander
        # reported stalled:false and nobody was told to act. Reported by mac-a
        # after sitting behind a holder that had been dead three hours: the
        # original deadlock, arriving through the flag meant to catch it.
        holder_gone = (
            held_by is not None
            and holder != HUMAN_FLOOR
            and not held_by["live"]
            and not held_by["listening"]
        )
        if waited >= self.REST_AFTER_EMPTY_WAITS and not me_holds and not holder_active:
            advice = (
                f"You've waited {waited} times with nothing arriving. STOP polling — "
                "tell the user the other side hasn't answered and end your turn. Your "
                "turn stays in the room; check once when they next speak to you."
            )
        elif room.awaiting_human:
            advice = (
                "Blocked on the PERSON, not on any agent. Put this question to the "
                f"user in chat and relay their answer with kb_room_relay_answer: "
                f"{room.awaiting_human!r}"
            )
        elif me_holds:
            advice = "Your turn. Nobody else will speak until you do — answer."
        elif not others:
            advice = "Nobody else has ever joined. Don't wait; work alone and say so."
        elif holder_gone:
            # Say what to DO, not merely what is true. "Not currently listening"
            # reads as stepped-away-mid-turn; the fact is they are gone and the
            # thread cannot move until somebody takes the floor back. Posting out
            # of turn is always allowed, so this is advice the reader can act on
            # immediately.
            advice = (
                f"{holder_name} holds the floor and has GONE — nothing can advance "
                "until someone takes it. Take it: post, and the floor moves on."
            )
        elif not [s for s in others if s["live"] or s["listening"]]:
            advice = (
                "They were here and have gone quiet. Don't wait — leave your turn "
                "and do something useful."
            )
        elif held_by is not None and held_by["listening"]:
            advice = (
                f"{holder_name} holds the floor and is listening right now — a reply "
                "is genuinely coming. Waiting is correct."
            )
        elif any(s["listening"] for s in others):
            # Someone is parked, but NOT the person who owes the reply. Saying "they
            # hold the floor and are listening" here was simply false — reported by
            # mac-b, who checked the sentence against the rows instead of trusting
            # it. Precisely the assembly error do_next exists to remove, reproduced
            # inside do_next itself.
            advice = (
                f"{holder_name or 'Someone else'} holds the floor and is NOT currently "
                "listening; another session is. Don't read that as a reply coming."
            )
        else:
            advice = (
                f"{holder_name or 'Someone else'} holds the floor. They know it's "
                "their turn; don't re-post."
            )
        return {
            "do_next": advice,
            "holder": holder,
            "holder_name": holder_name,
            "is_you": bool(holder) and holder == me,
            "open": not holder,
            "speakers": speakers,
            "others": others,
            # Three distinct reasons a reply might not come, which agents kept
            # conflating into one hopeful "they must be thinking":
            "alone": not others,          # nobody else was ever here
            # "cannot advance": either nobody live is left, or the one person who
            # owes the turn has gone. The second half is what mac-a caught.
            "stalled": bool(others) and (not live_others or holder_gone),
            "awaiting_human": room.awaiting_human,          # blocked on a person
            "empty_waits": waited,   # polls in a row that returned nothing
            "anyone_listening": any(s["listening"] for s in others),
            "since": room.floor_since,
        }

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
        speaker: str = "", hand_to: str | None = None,
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
            if kind == "message" and speaker:
                # Speaking ends your turn. Whoever just talked is by definition no
                # longer waiting, so the floor moves on the same write that
                # records the turn — never as a separate call an agent can forget.
                self._conn.execute(
                    "UPDATE room_speakers SET listening_until = '', last_spoke = ? "
                    "WHERE room_id = ? AND speaker = ?",
                    (_now(), room_id, speaker),
                )
                self._pass_floor_locked(room_id, speaker, hand_to)
            elif kind == "message" and _is_human_session(session):
                # The person answered. Unblock the room and give the floor back to
                # whichever session asked, so the answer reaches the one waiting
                # on it instead of landing in an open room nobody owns.
                self._human_answered_locked(room_id)
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
                # COUNT the turns past your cursor — do not subtract ids. Turn ids
                # are a single global sequence across every room, so `last_id -
                # read_floor` is not a count of anything: a brand-new room reported
                # 70 unread on one system turn, because its only turn happened to be
                # the 70th written server-wide. It read as 0 in rooms you were caught
                # up in (there the two happen to be equal) which is exactly why it
                # survived this long.
                unread = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM room_turns WHERE room_id = ? AND id > ?",
                    (room.id, read_floor),
                ).fetchone()["n"]
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


async def room_wait_any(room_ids: list[int], seconds: int) -> int | None:
    """Block until ANY of these rooms sees a turn; return the one that woke us.

    A session in several rooms otherwise has to pick one to block on and goes deaf
    to the others — or polls, which is what the whole bus exists to avoid. Returns
    None on timeout. Losing waiters are cancelled, so a session watching ten rooms
    still costs one wakeup, not ten."""
    ids = [int(r) for r in room_ids]
    if not ids:
        return None

    # No shortcut for the single-room case: delegating to room_wait() there would
    # have to return the id unconditionally, which reports a wake on a plain
    # timeout — a caller then believes a turn arrived when none did.
    async def _one(rid: int) -> int:
        cond = _room_condition(rid)
        async with cond:
            await cond.wait()
        return rid

    tasks = {_asyncio.ensure_future(_one(rid)): rid for rid in ids}
    try:
        done, pending = await _asyncio.wait(
            tasks.keys(), timeout=max(1, min(120, seconds)),
            return_when=_asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    for t in done:
        try:
            return t.result()
        except Exception:  # noqa: BLE001 — a cancelled/failed waiter is not a wake
            continue
    return None
