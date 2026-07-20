"""Tenancy store — accounts + invites for multi-user Engram (v2 M0.1).

One SQLite file (WAL) holds who exists and who is invited; brains stay git.
This module is MECHANISM only: it enforces integrity (unique handles/emails/
subjects, invite lifecycle, reserved handles) but not sign-in POLICY. Policy —
e.g. "a Google sign-in must present the invited email, a GitHub sign-in proves
itself by possessing the emailed magic link" — belongs to the M1 onboarding
flow, which composes these primitives.

Identity key is the OAuth proxy's subject string ("github:metalfinger",
"google:someone@example.com") — minted in oauth/provider.subject_for and
carried on every access token, so subject -> user -> store is the whole
authz chain (M0.4).

Sync API by design: calls are microsecond-scale row operations guarded by one
process-wide lock, safe to call from async code without a threadpool.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import KBError


class TenancyError(KBError):
    """Account/invite misuse; the message teaches the caller the correct next step."""


# Handles double as the federated address later (hiren@engram...) and as the
# per-user directory name under users_root — keep them boring: lowercase
# kebab, 2-32 chars, no leading/trailing hyphen.
_HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

RESERVED_HANDLES = frozenset(
    {
        "admin", "administrator", "all", "api", "brain", "brains", "contact",
        "dashboard", "engram", "everyone", "help", "hiren", "info", "invite",
        "invites", "mail", "mcp", "me", "metalfinger", "oauth", "root",
        "security", "share", "support", "system", "team", "www",
    }
)

_INVITE_TTL_DAYS = 14


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_from_now(days: int) -> str:
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_handle(handle: str) -> str:
    """Normalize + validate a handle; returns the canonical (lowercase) form."""
    normalized = handle.strip().lower()
    if len(normalized) < 2 or len(normalized) > 32:
        raise TenancyError(
            f"Handle {handle!r} must be 2-32 characters (lowercase letters, digits, hyphens)."
        )
    if not _HANDLE_RE.match(normalized):
        raise TenancyError(
            f"Handle {handle!r} is invalid: lowercase letters, digits and inner hyphens only "
            "(no leading/trailing hyphen)."
        )
    if normalized in RESERVED_HANDLES:
        raise TenancyError(f"Handle {handle!r} is reserved — pick another.")
    return normalized


@dataclass(frozen=True)
class User:
    id: int
    handle: str
    email: str
    idp: str
    idp_subject: str
    status: str  # active | suspended
    created: str


@dataclass(frozen=True)
class Invite:
    token: str
    email: str
    preset: str
    invited_by: int | None
    created: str
    expires: str
    accepted_by: int | None
    revoked: bool

    @property
    def live(self) -> bool:
        return (
            not self.revoked
            and self.accepted_by is None
            and self.expires > _now()
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    idp TEXT NOT NULL,
    idp_subject TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE,
    preset TEXT NOT NULL DEFAULT 'member',
    invited_by INTEGER REFERENCES users(id),
    created TEXT NOT NULL,
    expires TEXT NOT NULL,
    accepted_by INTEGER REFERENCES users(id),
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


class TenancyStore:
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

    # -- users ---------------------------------------------------------------

    def _user(self, row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(
            id=row["id"], handle=row["handle"], email=row["email"],
            idp=row["idp"], idp_subject=row["idp_subject"],
            status=row["status"], created=row["created"],
        )

    def user_by_subject(self, subject: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE idp_subject = ?", (subject,)
            ).fetchone()
        return self._user(row)

    def user_by_handle(self, handle: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE handle = ?", (handle.strip().lower(),)
            ).fetchone()
        return self._user(row)

    def user_by_email(self, email: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.strip(),)
            ).fetchone()
        return self._user(row)

    def list_users(self) -> list[User]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [self._user(r) for r in rows]  # type: ignore[misc]

    def set_status(self, handle: str, status: str) -> None:
        if status not in ("active", "suspended"):
            raise TenancyError(f"Unknown status {status!r} (active | suspended).")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET status = ? WHERE handle = ?",
                (status, handle.strip().lower()),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise TenancyError(f"No user with handle {handle!r}.")

    def _insert_user(
        self, handle: str, email: str, idp: str, idp_subject: str
    ) -> User:
        """Insert under the caller-held lock-free path; callers hold self._lock."""
        try:
            cur = self._conn.execute(
                "INSERT INTO users (handle, email, idp, idp_subject, status, created) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (handle, email.strip(), idp, idp_subject, _now()),
            )
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "users.handle" in msg:
                raise TenancyError(f"Handle {handle!r} is already taken.") from exc
            if "users.email" in msg:
                raise TenancyError(f"Email {email!r} already has an account.") from exc
            if "users.idp_subject" in msg:
                raise TenancyError(
                    f"Identity {idp_subject!r} already has an account."
                ) from exc
            raise
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return self._user(row)  # type: ignore[return-value]

    def bootstrap_owner(
        self, handle: str, email: str, idp: str, idp_subject: str
    ) -> User:
        """Create user #1 (the operator) without an invite. Idempotent on subject."""
        existing = self.user_by_subject(idp_subject)
        if existing is not None:
            return existing
        canonical = handle.strip().lower()  # owner may claim a reserved handle
        if not _HANDLE_RE.match(canonical):
            raise TenancyError(f"Handle {handle!r} is invalid.")
        with self._lock:
            user = self._insert_user(canonical, email, idp, idp_subject)
            self._conn.commit()
        return user

    # -- invites -------------------------------------------------------------

    def _invite(self, row: sqlite3.Row | None) -> Invite | None:
        if row is None:
            return None
        return Invite(
            token=row["token"], email=row["email"], preset=row["preset"],
            invited_by=row["invited_by"], created=row["created"],
            expires=row["expires"], accepted_by=row["accepted_by"],
            revoked=bool(row["revoked"]),
        )

    def create_invite(
        self,
        email: str,
        preset: str = "member",
        invited_by: int | None = None,
        ttl_days: int = _INVITE_TTL_DAYS,
    ) -> Invite:
        email = email.strip()
        if "@" not in email or len(email) < 5:
            raise TenancyError(f"{email!r} does not look like an email address.")
        if self.user_by_email(email) is not None:
            raise TenancyError(f"{email} already has an account — no invite needed.")
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._conn.execute(
                "INSERT INTO invites (token, email, preset, invited_by, created, expires) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, email, preset, invited_by, _now(), _days_from_now(ttl_days)),
            )
            self._conn.commit()
        invite = self.get_invite(token)
        assert invite is not None
        return invite

    def get_invite(self, token: str) -> Invite | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invites WHERE token = ?", (token,)
            ).fetchone()
        return self._invite(row)

    def list_invites(self, live_only: bool = False) -> list[Invite]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM invites ORDER BY created DESC"
            ).fetchall()
        invites = [self._invite(r) for r in rows]
        if live_only:
            invites = [i for i in invites if i and i.live]
        return [i for i in invites if i is not None]

    def revoke_invite(self, token: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE invites SET revoked = 1 WHERE token = ? AND accepted_by IS NULL",
                (token,),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise TenancyError("No revocable invite for that token (unknown or already accepted).")

    def accept_invite(
        self, token: str, handle: str, email: str, idp: str, idp_subject: str
    ) -> User:
        """Redeem a live invite into an account — one transaction, no half-states.

        `email` is what gets recorded for the user (normally the invite's email);
        whether the signing-in identity may claim this invite is M1 policy, not
        enforced here.
        """
        canonical = validate_handle(handle)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invites WHERE token = ?", (token,)
            ).fetchone()
            invite = self._invite(row)
            if invite is None:
                raise TenancyError("Unknown invite token.")
            if invite.revoked:
                raise TenancyError("This invite was revoked.")
            if invite.accepted_by is not None:
                raise TenancyError("This invite was already used.")
            if invite.expires <= _now():
                raise TenancyError("This invite has expired — ask for a fresh one.")
            user = self._insert_user(canonical, email, idp, idp_subject)
            self._conn.execute(
                "UPDATE invites SET accepted_by = ? WHERE token = ?",
                (user.id, token),
            )
            self._conn.commit()
        return user
