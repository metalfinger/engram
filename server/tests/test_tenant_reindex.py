"""Nightly tenant reindex + duplicate checks that ignore non-concepts (2026-10-10).

Both found while switching search to EmbeddingGemma: the nightly reconcile only
re-embedded the owner's brain, so other accounts' older concepts were missing from the
new collection; and the duplicate checks matched concepts against their own project's
log/context/messages, which embed close to what they summarise.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.scheduler import Scheduler


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s

# -- scheduler wiring ------------------------------------------------------------


async def _noop():
    return None


def _job_names(sched: Scheduler) -> list[str]:
    sched._thread.start = lambda: None  # don't actually spin the daemon in tests
    sched.start()
    return [name for name, _t, _fn in sched._jobs]


def test_tenant_reindex_is_scheduled_alongside_the_reconcile():
    s = Settings(_env_file=None, reconcile_at="03:30", briefing_at="", presence_ingest_seconds=0)
    sched = Scheduler(SimpleNamespace(semantic=None), s, asyncio.new_event_loop(), tenant_reindex=_noop)
    assert _job_names(sched) == ["reconcile", "tenant-reindex"]


def test_no_tenant_job_without_the_hook():
    s = Settings(_env_file=None, reconcile_at="03:30", briefing_at="", presence_ingest_seconds=0)
    sched = Scheduler(SimpleNamespace(semantic=None), s, asyncio.new_event_loop())
    assert _job_names(sched) == ["reconcile"]


# -- _reindex_tenants -------------------------------------------------------------


class _Store:
    def __init__(self, handle: str, calls: list[str], boom: bool = False) -> None:
        self.root = f"/brains/{handle}"
        self.semantic = SimpleNamespace(full_reindex=self._reindex)
        self._handle, self._calls, self._boom = handle, calls, boom

    def _reindex(self, root):
        if self._boom:
            raise RuntimeError("qdrant down")
        self._calls.append(self._handle)
        return {"indexed": 1}


@pytest.mark.asyncio
async def test_every_other_active_account_is_reindexed_and_one_failure_does_not_stop_it(monkeypatch):
    calls: list[str] = []
    users = [
        SimpleNamespace(handle="hiren", status="active"),     # the owner: reconcile covers it
        SimpleNamespace(handle="alice", status="active"),
        SimpleNamespace(handle="bob", status="suspended"),    # suspended: skipped
        SimpleNamespace(handle="carol", status="active"),     # fails
        SimpleNamespace(handle="dave", status="active"),      # must still run after carol
    ]

    async def store_for_handle(handle):
        return _Store(handle, calls, boom=(handle == "carol"))

    monkeypatch.setattr(app_module, "settings", app_module.settings.model_copy(
        update={"multiuser": True, "owner_handle": "hiren"}))
    monkeypatch.setattr(app_module, "registry", SimpleNamespace(
        tenancy=SimpleNamespace(list_users=lambda: users), store_for_handle=store_for_handle))

    out = await app_module._reindex_tenants()

    assert calls == ["alice", "dave"]
    assert "error" in out["carol"]
    assert "hiren" not in out and "bob" not in out


@pytest.mark.asyncio
async def test_single_user_mode_has_no_tenants(monkeypatch):
    monkeypatch.setattr(app_module, "settings", app_module.settings.model_copy(update={"multiuser": False}))
    assert await app_module._reindex_tenants() == {}


# -- duplicate checks skip anchors and session mail -------------------------------


@pytest.mark.asyncio
async def test_kb_write_never_calls_a_projects_own_log_a_duplicate(store):
    store.semantic = SimpleNamespace(
        search=lambda q, limit=6: [
            {"path": "projects/alt/log.md", "score": 0.97},
            {"path": "projects/alt/context.md", "score": 0.95},
            {"path": "projects/alt/messages/2026-10-01-hi.md", "score": 0.94},
            {"path": "projects/alt/specs/the-real-twin.md", "score": 0.91},
        ],
        dupe_threshold=0.80,
    )
    r = await store.kb_write(
        "projects/alt/ideas/new-thing.md",
        "---\ntype: idea\ndescription: A thought.\n---\n\nSee [ctx](../context.md).\n",
        "add",
    )
    dupes = [w for w in r["warnings"] if "duplicate" in w]
    assert len(dupes) == 1 and "the-real-twin.md" in dupes[0], dupes


@pytest.mark.asyncio
async def test_reconcile_pairs_skip_anchors_but_still_find_the_real_pair(store):
    from engram_server.reconcile import _similar_pairs

    a = "---\ntype: idea\ntitle: Alpha\ndescription: d\n---\n\nbody\n"
    await store.kb_write("projects/alt/ideas/alpha.md", a, "a")
    await store.kb_write("projects/alt/ideas/beta.md", a.replace("Alpha", "Beta"), "b")

    def search(query, limit=6):
        twin = "projects/alt/ideas/beta.md" if "Alpha" in query else "projects/alt/ideas/alpha.md"
        return [  # anchors take the top slots, the real twin comes third
            {"path": "projects/alt/log.md", "score": 0.99},
            {"path": "projects/alt/context.md", "score": 0.98},
            {"path": twin, "score": 0.93},
        ]

    pairs = _similar_pairs(store.root, SimpleNamespace(search=search), 0.80)
    paths = {p for a_, b_, _s in pairs for p in (a_, b_)}
    assert ("projects/alt/ideas/alpha.md" in paths) and ("projects/alt/ideas/beta.md" in paths)
    assert not any(p.endswith(("log.md", "context.md")) for p in paths)
