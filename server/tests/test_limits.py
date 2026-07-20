"""M0.7 — per-tenant quotas + rate limits (engram_server.limits).

Zero-network: RateLimiter/QuotaCache take injectable clocks so windows/TTLs
advance deterministically without sleeping. enforce() is exercised against a
real provisioned tenant brain (provisioning.ensure_user_brain) so store.root is
a real on-disk checkout, matching how current_store() will call it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram_server.limits import (
    QuotaCache,
    QuotaError,
    RateLimitError,
    RateLimiter,
    dir_size_bytes,
    enforce,
)
from engram_server.provisioning import ensure_user_brain, user_settings


class FakeClock:
    """Monotonic-shaped fake clock: starts at 0, advances only when told to."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ------------------------------------------------------------------ RateLimiter


def test_check_thread_post_uses_own_budget_and_disables_at_zero():
    from engram_server.limits import RateLimitError, RateLimiter, check_thread_post

    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    for _ in range(3):
        check_thread_post("google:a@example.com", 3, limiter=limiter)
    with pytest.raises(RateLimitError):
        check_thread_post("google:a@example.com", 3, limiter=limiter)
    # a different subject has its own budget
    check_thread_post("google:b@example.com", 3, limiter=limiter)
    # limit 0 disables entirely
    for _ in range(50):
        check_thread_post("google:c@example.com", 0, limiter=limiter)


def test_rate_limiter_allows_up_to_the_limit():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("alice", 5)  # must not raise


def test_rate_limiter_blocks_the_next_call_over_limit():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("alice", 5)
    with pytest.raises(RateLimitError):
        limiter.check("alice", 5)


def test_rate_limiter_resets_after_window_advances():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("alice", 5)
    with pytest.raises(RateLimitError):
        limiter.check("alice", 5)

    clock.advance(61.0)
    limiter.check("alice", 5)  # new window — must not raise


def test_rate_limiter_subjects_have_independent_budgets():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("alice", 5)
    with pytest.raises(RateLimitError):
        limiter.check("alice", 5)

    limiter.check("bob", 5)  # bob's budget is untouched by alice's usage


# ------------------------------------------------------------------ dir_size_bytes


def test_dir_size_bytes_sums_known_file_sizes(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    (tmp_path / "b.txt").write_bytes(b"y" * 250)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"z" * 10)

    assert dir_size_bytes(tmp_path) == 360


def test_dir_size_bytes_include_git_toggle_changes_result(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "pack").write_bytes(b"g" * 500)

    with_git = dir_size_bytes(tmp_path, include_git=True)
    without_git = dir_size_bytes(tmp_path, include_git=False)

    assert with_git == 600
    assert without_git == 100
    assert with_git != without_git


# ------------------------------------------------------------------ QuotaCache


def test_quota_cache_returns_cached_value_within_ttl(tmp_path: Path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    clock = FakeClock()
    cache = QuotaCache(ttl_seconds=10.0, clock=clock)

    calls = {"n": 0}
    real_dir_size_bytes = dir_size_bytes

    def spy(path, include_git=True):
        calls["n"] += 1
        return real_dir_size_bytes(path, include_git=include_git)

    monkeypatch.setattr("engram_server.limits.dir_size_bytes", spy)

    first = cache.size_bytes(tmp_path)
    assert calls["n"] == 1

    clock.advance(5.0)  # still within TTL
    second = cache.size_bytes(tmp_path)
    assert calls["n"] == 1  # not recomputed
    assert second == first


def test_quota_cache_recomputes_after_ttl_expires(tmp_path: Path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    clock = FakeClock()
    cache = QuotaCache(ttl_seconds=10.0, clock=clock)

    calls = {"n": 0}
    real_dir_size_bytes = dir_size_bytes

    def spy(path, include_git=True):
        calls["n"] += 1
        return real_dir_size_bytes(path, include_git=include_git)

    monkeypatch.setattr("engram_server.limits.dir_size_bytes", spy)

    cache.size_bytes(tmp_path)
    assert calls["n"] == 1

    clock.advance(11.0)  # past TTL
    (tmp_path / "b.txt").write_bytes(b"y" * 50)  # prove it actually re-walked
    result = cache.size_bytes(tmp_path)
    assert calls["n"] == 2
    assert result == 150


# ------------------------------------------------------------------ enforce()


class _FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root


def _tenant_store(settings, handle: str):
    brain = ensure_user_brain(settings, handle)
    return _FakeStore(brain.checkout)


def test_enforce_passes_under_quota_and_under_rate(settings, tmp_path):
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
            "tenant_quota_mb": 200,
            "tenant_rate_per_min": 120,
        }
    )
    store = _tenant_store(mu, "alice")
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    cache = QuotaCache(clock=clock)

    enforce(store, "alice", mu, rate_limiter=limiter, quota_cache=cache)  # must not raise


def test_enforce_raises_quota_error_when_over_quota(settings, tmp_path):
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
            "tenant_quota_mb": 0,  # start unlimited so provisioning + write succeed
            "tenant_rate_per_min": 120,
        }
    )
    store = _tenant_store(mu, "alice")
    # Pad the checkout past a tiny quota we'll enforce against.
    (store.root / "padding.bin").write_bytes(b"0" * (1024 * 1024))

    tight = mu.model_copy(update={"tenant_quota_mb": 1})  # 1MB ceiling, we wrote >=1MB
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    cache = QuotaCache(clock=clock)

    with pytest.raises(QuotaError):
        enforce(store, "alice", tight, rate_limiter=limiter, quota_cache=cache)


def test_enforce_raises_rate_limit_error_when_over_rate(settings, tmp_path):
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
            "tenant_quota_mb": 200,
            "tenant_rate_per_min": 2,
        }
    )
    store = _tenant_store(mu, "alice")
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    cache = QuotaCache(clock=clock)

    enforce(store, "alice", mu, rate_limiter=limiter, quota_cache=cache)
    enforce(store, "alice", mu, rate_limiter=limiter, quota_cache=cache)
    with pytest.raises(RateLimitError):
        enforce(store, "alice", mu, rate_limiter=limiter, quota_cache=cache)


def test_enforce_is_bypassed_for_owner_via_is_owner_flag(settings, tmp_path):
    """enforce() takes an is_owner belt-and-suspenders guard: even if a caller
    accidentally passes the owner store/subject, it's a no-op rather than
    rate-limiting or quota-blocking Hiren. The primary contract is still that
    current_store() never calls enforce() for the owner path at all."""
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
            "tenant_quota_mb": 1,
            "tenant_rate_per_min": 1,
        }
    )
    owner_store = _FakeStore(mu.brain_path)  # not even provisioned — proves no check runs
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    cache = QuotaCache(clock=clock)

    for _ in range(5):
        enforce(
            owner_store,
            "github:metalfinger",
            mu,
            is_owner=True,
            rate_limiter=limiter,
            quota_cache=cache,
        )  # must never raise


def test_enforce_skips_rate_limit_when_configured_off(settings, tmp_path):
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
            "tenant_quota_mb": 200,
            "tenant_rate_per_min": 0,  # off
        }
    )
    store = _tenant_store(mu, "alice")
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    cache = QuotaCache(clock=clock)

    for _ in range(50):
        enforce(store, "alice", mu, rate_limiter=limiter, quota_cache=cache)


def test_enforce_skips_quota_when_configured_off(settings, tmp_path):
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
            "tenant_quota_mb": 0,  # off
            "tenant_rate_per_min": 120,
        }
    )
    store = _tenant_store(mu, "alice")
    (store.root / "padding.bin").write_bytes(b"0" * (5 * 1024 * 1024))
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    cache = QuotaCache(clock=clock)

    enforce(store, "alice", mu, rate_limiter=limiter, quota_cache=cache)  # must not raise
