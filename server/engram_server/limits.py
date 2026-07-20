"""Per-tenant quotas + rate limits (M0.7) — the seam ``current_store()`` calls for
every NON-owner caller, right after a tenant store is resolved and before it's
handed back to the tool body. The owner is never subject to any of this; enforce()
below takes an ``is_owner`` flag as a belt-and-suspenders guard, but the primary
contract is that the caller (current_store) simply never calls enforce() for the
owner path in the first place.

Two independent gates:
  - RateLimiter: tool calls per minute per token subject (in-memory, fixed window).
  - dir_size_bytes + QuotaCache: on-disk ceiling for a tenant's checkout, TTL-cached
    so a busy session doesn't re-walk the tree on every single call.

Both are no-ops when their config knob is <= 0 (see enforce()).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Settings
from .errors import KBError


class RateLimitError(KBError):
    """Subject exceeded tenant_rate_per_min; the message teaches them to slow down."""


class QuotaError(KBError):
    """Tenant checkout is at/over tenant_quota_mb; the message teaches prune-and-retry."""


# ------------------------------------------------------------------ rate limiting


class RateLimiter:
    """Fixed-window (per-minute) call counter, keyed by token subject.

    Fixed windows (rather than a sliding log) are enough here: the failure mode
    we're guarding against is "one tenant hammers tools in a loop", not precise
    fairness at the window boundary. Injectable ``clock`` (default
    ``time.monotonic``) so tests can advance time deterministically without
    sleeping — production never passes one.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        # subject -> (window_start, count)
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, subject: str, limit_per_min: int) -> None:
        """Record one call for ``subject``; raise RateLimitError if that puts them
        over ``limit_per_min`` calls within the current 60s window."""
        now = self._clock()
        window_start, count = self._windows.get(subject, (now, 0))
        if now - window_start >= 60.0:
            window_start, count = now, 0
        count += 1
        if count > limit_per_min:
            raise RateLimitError(
                f"Rate limit hit ({limit_per_min}/min) — you're calling tools too "
                "fast. Wait a moment and try again."
            )
        self._windows[subject] = (window_start, count)


# ------------------------------------------------------------------ disk quota


def dir_size_bytes(path: Path, include_git: bool = True) -> int:
    """Sum of file sizes under ``path``.

    ``include_git`` defaults True: disk is disk, and a tenant's ``.git`` history
    is real bytes sitting on this shared PC's drive regardless of whether the
    working tree shrinks. The tradeoff is that a tenant who commits a huge file
    and then deletes it in a later commit still counts against quota via
    history — which is the point (it stops "delete it in HEAD to dodge quota"
    as an escape hatch). Pass ``include_git=False`` if a future caller wants a
    working-tree-only view (e.g. a UI that shows "your files" separately from
    "your git history").
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        if not include_git and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue  # vanished mid-walk (concurrent write) — not our problem here
    return total


class QuotaCache:
    """dir_size_bytes(path), cached per-path with a short TTL.

    Walking a whole checkout on every tool call would be wasteful; a busy
    session firing several calls a second only needs the size to be roughly
    fresh. Injectable clock for tests, same reasoning as RateLimiter.
    """

    def __init__(self, ttl_seconds: float = 10.0, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, int]] = {}

    def size_bytes(self, path: Path, include_git: bool = True) -> int:
        key = str(path)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        size = dir_size_bytes(path, include_git=include_git)
        self._cache[key] = (now, size)
        return size


# Module-level singletons: one process, one rate table, one quota cache. Tests
# construct their own RateLimiter/QuotaCache instances with fake clocks instead
# of reaching into these, so production state never leaks into a test.
_rate_limiter = RateLimiter()
_quota_cache = QuotaCache()


# ------------------------------------------------------------------ composed enforcement


def enforce(
    store,
    subject: str,
    settings: Settings,
    *,
    is_owner: bool = False,
    rate_limiter: RateLimiter | None = None,
    quota_cache: QuotaCache | None = None,
) -> None:
    """The gate current_store() calls for a resolved tenant store, before handing
    it back. Rate-limits ``subject`` first (cheap, catches hammering fast), then
    quota-checks ``store.root`` (cache-backed, catches disk exhaustion).

    ``is_owner`` is a belt-and-suspenders guard: the intended usage is that
    current_store() simply never calls enforce() for the owner store at all
    (owner returns early, before this seam), so this flag should never see True
    in production. It's here so a call site that accidentally passes the owner
    store fails safe (no-op) instead of rate-limiting/quota-blocking Hiren.

    Quota is enforced as a blanket gate (reads included), not just on writes:
    current_store() resolves the store before the tool body runs, so at this
    point we don't yet know whether the call is a read or a write. Blocking
    reads too when already over quota is the simplest correct behavior and
    still leaves a way out — the message below tells the model to prune via
    kb_edit/kb_move, both of which go through this same store and are the
    tenant's only way to shrink their checkout without operator help.
    """
    if is_owner:
        return

    limiter = rate_limiter if rate_limiter is not None else _rate_limiter
    cache = quota_cache if quota_cache is not None else _quota_cache

    if settings.tenant_rate_per_min > 0:
        limiter.check(subject, settings.tenant_rate_per_min)

    if settings.tenant_quota_mb > 0:
        used = cache.size_bytes(store.root)
        limit_bytes = settings.tenant_quota_mb * 1024 * 1024
        if used >= limit_bytes:
            used_mb = used / (1024 * 1024)
            raise QuotaError(
                f"Your brain checkout is at {used_mb:.1f}MB, over the "
                f"{settings.tenant_quota_mb}MB quota — delete or shrink content "
                "before continuing (kb_edit to trim a concept, kb_move to "
                "consolidate, or ask the operator to raise your quota)."
            )
