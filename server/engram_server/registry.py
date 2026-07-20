"""Per-user store registry (v2 M0.3) — the seam that replaces the single global KBStore.

One registry per process. The operator's store is constructed exactly the way the
old module-global was (same settings, same GitHub remote) so every single-user
surface — explorer, office, scheduler, presence spool — keeps working against
``registry.owner`` unchanged. Tenant stores are built lazily on first resolve:
subject -> tenancy row -> provision brain (M0.2) -> KBStore over the user's own
checkout, cached for the process lifetime.

With ``multiuser=False`` (the default) every subject resolves to the owner store —
byte-for-byte the old behavior, which is what keeps the existing 600-test suite
meaningful while M0 lands piecewise.

AuthZ note (M0.4 builds on this): ``store_for_subject`` is the ONLY way a request
identity becomes a store. Nothing here trusts a handle from user input — handles
come out of the tenancy DB, subjects come off the validated access token.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from anyio import to_thread

from .config import Settings
from .errors import KBError
from .capabilities import CapabilityStore
from .kbstore import KBStore
from .provisioning import ensure_user_brain, user_settings
from .social import SocialStore
from .tenancy import TenancyStore


def tenancy_db_path(settings: Settings) -> Path:
    return (
        Path(settings.tenancy_db_path)
        if settings.tenancy_db_path
        else Path.home() / ".engram" / "engram.db"
    )


class StoreRegistry:
    def __init__(self, settings: Settings, owner: KBStore | None = None) -> None:
        self._settings = settings
        self.owner = owner if owner is not None else KBStore(settings)
        self._stores: dict[str, KBStore] = {}
        self._construct_lock = asyncio.Lock()
        self._tenancy: TenancyStore | None = None
        self._social: SocialStore | None = None
        self._capabilities: CapabilityStore | None = None
        self._owner_subjects = frozenset(
            s.strip() for s in settings.owner_subjects.split(",") if s.strip()
        )

    @property
    def tenancy(self) -> TenancyStore:
        """Lazy: the SQLite file is only ever created in multiuser mode."""
        if self._tenancy is None:
            self._tenancy = TenancyStore(tenancy_db_path(self._settings))
        return self._tenancy

    @property
    def social(self) -> SocialStore:
        """Contacts/DMs/notifications over the SAME engram.db as tenancy. Touch
        tenancy first so its `users` table exists before social's FKs reference it."""
        if self._social is None:
            _ = self.tenancy  # ensure users table is created
            self._social = SocialStore(tenancy_db_path(self._settings))
        return self._social

    @property
    def capabilities(self) -> CapabilityStore:
        """Share requests + capability grants over the SAME engram.db (M3)."""
        if self._capabilities is None:
            _ = self.tenancy
            self._capabilities = CapabilityStore(tenancy_db_path(self._settings))
        return self._capabilities

    def tenancy_handle_map(self) -> dict[int, str]:
        """{user_id: handle} for rendering social ids as @handles in tool output."""
        return {u.id: u.handle for u in self.tenancy.list_users()}

    def ensure_owner_account(self):
        """Bootstrap the operator as a first-class account so they join the social
        graph (contacts/DMs/notifications) like any user. Idempotent; owner keeps
        mapping to the original brain via owner_subjects regardless."""
        subject = next(iter(sorted(self._owner_subjects)), None)
        if subject is None:
            return None
        idp = subject.split(":", 1)[0]
        email = self._settings.owner_email or f"{self._settings.owner_handle}@engram.local"
        return self.tenancy.bootstrap_owner(
            self._settings.owner_handle, email, idp, subject
        )

    def cached_handles(self) -> list[str]:
        """Handles with a live store this process (for iteration/diagnostics)."""
        return sorted(self._stores)

    async def store_for_subject(self, subject: str) -> KBStore:
        """The authz chain: validated token subject -> account -> that user's store."""
        if not self._settings.multiuser:
            return self.owner
        if subject in self._owner_subjects:
            return self.owner
        user = self.tenancy.user_by_subject(subject)
        if user is None:
            raise KBError(
                f"No Engram account for {subject!r}. Accounts are invite-only — "
                "ask the operator for an invite, then sign in again once accepted."
            )
        if user.status != "active":
            raise KBError(
                f"The account @{user.handle} is suspended — contact the operator."
            )
        if user.handle == self._settings.owner_handle:
            return self.owner
        return await self._store_for_handle(user.handle)

    async def store_for_handle(self, handle: str) -> KBStore:
        """The store for a NAMED user (M3 guest reads read someone else's brain).
        Owner handle -> the original brain; an active tenant -> their provisioned
        brain. Raises if the handle is unknown or suspended. Authorization that the
        CALLER may read this store is enforced separately (capability check)."""
        if handle == self._settings.owner_handle:
            return self.owner
        user = self.tenancy.user_by_handle(handle)
        if user is None:
            raise KBError(f"No Engram user @{handle}.")
        if user.status != "active":
            raise KBError(f"@{handle}'s account is not active.")
        return await self._store_for_handle(user.handle)

    async def _store_for_handle(self, handle: str) -> KBStore:
        store = self._stores.get(handle)
        if store is not None:
            return store
        async with self._construct_lock:
            store = self._stores.get(handle)  # lost the race -> reuse the winner's
            if store is not None:
                return store
            brain = await to_thread.run_sync(
                lambda: ensure_user_brain(self._settings, handle)
            )
            store = KBStore(user_settings(self._settings, brain))
            await store.start()
            self._stores[handle] = store
        return store
