"""Capability store — share requests + grants for multi-user Engram (v2 M3.1).

Engram's core social feature is sharing knowledge ACCESS: user A grants user B
scoped read access to part of A's git brain, so B's Claude can read A's shelf
directly. Neither side of that relationship lives inside either user's
isolated git brain, so it lives here, in the SAME neutral SQLite file
(engram.db) as tenancy.py and social.py, as a sibling table set referencing
users(id) by plain INTEGER. This module is MECHANISM only: share_requests
model the ask-then-approve handshake, capabilities model the resulting grant,
and `check()` is the enforcement primitive the guest-read tools call before
every read — but minting the capability on request-approval is the CALLER's
job (resolve_request only flips pending -> approved/denied).

Path prefixes (in both share_requests.paths and capabilities.paths) are
repo-relative POSIX prefixes into the OWNER's brain. Because a capability
that could name a path outside the owner's brain root would be a full
directory-traversal break, every path prefix is validated at the write
boundary (validate_path_prefix) and coverage is checked at the read boundary
(covers_path) with the same traversal exclusions — never trust either side
to have been validated by the other.

Sync API by design, same as TenancyStore/SocialStore: microsecond-scale row
operations guarded by one process-wide lock, safe to call from async code
without a threadpool.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import KBError

_ALLOWED_VERBS = frozenset({"read", "search", "browse"})


class CapabilityError(KBError):
    """Share-request/capability misuse; the message teaches the correct next step."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_from_now(days: int) -> str:
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_path_prefix(path: str) -> str:
    """Validate one repo-relative path prefix; returns it normalized (no
    leading/trailing '/'). Security boundary: a prefix must never be able to
    name anything outside the owner's brain root, so absolute paths,
    backslashes, and '..' are rejected outright rather than merely discouraged.
    """
    if not isinstance(path, str) or not path.strip():
        raise CapabilityError("Path prefix cannot be empty.")
    candidate = path.strip()
    if candidate.startswith("/"):
        raise CapabilityError(f"Path prefix {candidate!r} must be relative (no leading '/').")
    if "\\" in candidate:
        raise CapabilityError(f"Path prefix {candidate!r} must use forward slashes, not backslashes.")
    if ".." in candidate:
        raise CapabilityError(f"Path prefix {candidate!r} must not contain '..'.")
    return candidate.strip("/")


def _validate_paths(paths: list[str]) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise CapabilityError("At least one path prefix is required.")
    return [validate_path_prefix(p) for p in paths]


def _validate_verbs(verbs: list[str]) -> list[str]:
    if not isinstance(verbs, list) or not verbs:
        raise CapabilityError("At least one verb is required.")
    invalid = sorted(set(verbs) - _ALLOWED_VERBS)
    if invalid:
        raise CapabilityError(
            f"Unknown verb(s) {invalid} — allowed: {sorted(_ALLOWED_VERBS)}."
        )
    return list(verbs)


def covers_path(prefixes: list[str], path: str) -> bool:
    """True iff some prefix in `prefixes` covers `path`, boundary-safe.

    A granted prefix P covers X iff (normalized) X == P or X startswith
    P + "/" — so "projects/alt" covers "projects/alt" and
    "projects/alt/x.md" but NOT "projects/alt-secret" (no separator) and
    NOT "projects" (a parent is not covered by a child grant). Both sides
    are normalized by stripping leading/trailing "/" before comparing.
    A `path` that is absolute, contains '..', or contains a backslash never
    matches anything — never cover a traversal path.
    """
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/") or ".." in path or "\\" in path:
        return False
    normalized_path = path.strip("/")
    if not normalized_path:
        return False
    for prefix in prefixes:
        normalized_prefix = prefix.strip("/")
        if not normalized_prefix:
            continue
        if normalized_path == normalized_prefix or normalized_path.startswith(
            normalized_prefix + "/"
        ):
            return True
    return False


@dataclass(frozen=True)
class ShareRequest:
    id: int
    requester_id: int
    owner_id: int
    paths: list[str]
    reason: str
    status: str  # pending | approved | denied
    created: str
    resolved_at: str | None


@dataclass(frozen=True)
class Capability:
    id: int
    owner_id: int
    grantee_id: int
    paths: list[str]
    verbs: list[str]
    note: str
    token: str
    created: str
    expires: str
    revoked: bool

    @property
    def live(self) -> bool:
        return not self.revoked and self.expires > _now()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    owner_id INTEGER NOT NULL REFERENCES users(id),
    paths TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    grantee_id INTEGER NOT NULL REFERENCES users(id),
    paths TEXT NOT NULL,
    verbs TEXT NOT NULL,
    note TEXT,
    token TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    expires TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


class CapabilityStore:
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

    def _share_request(self, row: sqlite3.Row | None) -> ShareRequest | None:
        if row is None:
            return None
        return ShareRequest(
            id=row["id"], requester_id=row["requester_id"], owner_id=row["owner_id"],
            paths=json.loads(row["paths"]), reason=row["reason"] or "",
            status=row["status"], created=row["created"], resolved_at=row["resolved_at"],
        )

    def _capability(self, row: sqlite3.Row | None) -> Capability | None:
        if row is None:
            return None
        return Capability(
            id=row["id"], owner_id=row["owner_id"], grantee_id=row["grantee_id"],
            paths=json.loads(row["paths"]), verbs=json.loads(row["verbs"]),
            note=row["note"] or "", token=row["token"], created=row["created"],
            expires=row["expires"], revoked=bool(row["revoked"]),
        )

    # -- share requests (B asks A for access) ---------------------------------

    def create_request(
        self, requester_id: int, owner_id: int, paths: list[str], reason: str = ""
    ) -> ShareRequest:
        if requester_id == owner_id:
            raise CapabilityError("You cannot request access from yourself.")
        cleaned_paths = _validate_paths(paths)
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM share_requests WHERE requester_id = ? AND owner_id = ? "
                "AND status = 'pending'",
                (requester_id, owner_id),
            ).fetchone()
            if existing is not None:
                return self._share_request(existing)  # type: ignore[return-value]
            cur = self._conn.execute(
                "INSERT INTO share_requests "
                "(requester_id, owner_id, paths, reason, status, created, resolved_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, NULL)",
                (requester_id, owner_id, json.dumps(cleaned_paths), reason, _now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM share_requests WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._share_request(row)  # type: ignore[return-value]

    def list_incoming_requests(
        self, owner_id: int, pending_only: bool = True
    ) -> list[ShareRequest]:
        with self._lock:
            if pending_only:
                rows = self._conn.execute(
                    "SELECT * FROM share_requests WHERE owner_id = ? AND status = 'pending' "
                    "ORDER BY created DESC",
                    (owner_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM share_requests WHERE owner_id = ? ORDER BY created DESC",
                    (owner_id,),
                ).fetchall()
        return [self._share_request(r) for r in rows]  # type: ignore[misc]

    def list_outgoing_requests(
        self, requester_id: int, pending_only: bool = True
    ) -> list[ShareRequest]:
        with self._lock:
            if pending_only:
                rows = self._conn.execute(
                    "SELECT * FROM share_requests WHERE requester_id = ? AND status = 'pending' "
                    "ORDER BY created DESC",
                    (requester_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM share_requests WHERE requester_id = ? ORDER BY created DESC",
                    (requester_id,),
                ).fetchall()
        return [self._share_request(r) for r in rows]  # type: ignore[misc]

    def resolve_request(
        self, request_id: int, owner_id: int, approve: bool
    ) -> ShareRequest:
        """Flip a pending request to approved/denied. Minting the capability on
        approval is the CALLER's job — this only records the decision."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM share_requests WHERE id = ?", (request_id,)
            ).fetchone()
            request = self._share_request(row)
            if request is None:
                raise CapabilityError(f"No share request with id {request_id}.")
            if request.owner_id != owner_id:
                raise CapabilityError(
                    f"User {owner_id} does not own share request {request_id}."
                )
            if request.status != "pending":
                raise CapabilityError(
                    f"Share request {request_id} is already {request.status!r}."
                )
            new_status = "approved" if approve else "denied"
            self._conn.execute(
                "UPDATE share_requests SET status = ?, resolved_at = ? WHERE id = ?",
                (new_status, _now(), request_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM share_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return self._share_request(row)  # type: ignore[return-value]

    # -- capabilities (the actual grant) --------------------------------------

    def grant(
        self,
        owner_id: int,
        grantee_id: int,
        paths: list[str],
        verbs: list[str],
        days: int = 30,
        note: str = "",
    ) -> Capability:
        if owner_id == grantee_id:
            raise CapabilityError("You cannot grant a capability to yourself.")
        cleaned_paths = _validate_paths(paths)
        cleaned_verbs = _validate_verbs(verbs)
        token = secrets.token_urlsafe(24)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO capabilities "
                "(owner_id, grantee_id, paths, verbs, note, token, created, expires, revoked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    owner_id, grantee_id, json.dumps(cleaned_paths), json.dumps(cleaned_verbs),
                    note, token, _now(), _days_from_now(days),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM capabilities WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._capability(row)  # type: ignore[return-value]

    def revoke(self, capability_id: int, owner_id: int) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE capabilities SET revoked = 1 WHERE id = ? AND owner_id = ? AND revoked = 0",
                (capability_id, owner_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise CapabilityError(
                f"No live capability {capability_id} owned by user {owner_id} to revoke."
            )

    def list_granted_by(self, owner_id: int, live_only: bool = True) -> list[Capability]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM capabilities WHERE owner_id = ? ORDER BY created DESC",
                (owner_id,),
            ).fetchall()
        caps = [self._capability(r) for r in rows]  # type: ignore[misc]
        if live_only:
            caps = [c for c in caps if c.live]
        return caps

    def list_granted_to(self, grantee_id: int, live_only: bool = True) -> list[Capability]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM capabilities WHERE grantee_id = ? ORDER BY created DESC",
                (grantee_id,),
            ).fetchall()
        caps = [self._capability(r) for r in rows]  # type: ignore[misc]
        if live_only:
            caps = [c for c in caps if c.live]
        return caps

    def check(self, owner_id: int, grantee_id: int, path: str, verb: str) -> bool:
        """The enforcement primitive: True iff a LIVE capability from owner_id
        to grantee_id grants `verb` over `path`. Called before every guest read."""
        if verb not in _ALLOWED_VERBS:
            return False
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM capabilities WHERE owner_id = ? AND grantee_id = ? AND revoked = 0",
                (owner_id, grantee_id),
            ).fetchall()
        now = _now()
        for row in rows:
            if row["expires"] <= now:
                continue
            if verb not in json.loads(row["verbs"]):
                continue
            if covers_path(json.loads(row["paths"]), path):
                return True
        return False
