"""Nightly reconcile + brain-health report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engram_server.config import Settings
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore
from engram_server.reconcile import _housekeep, _scan, run_reconcile


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _write(root, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


async def test_scan_flags_dangling_orphan_and_dead(store: KBStore) -> None:
    root = store.root
    # a linked concept (has an inbound link from pointer) -> not orphan
    _write(root, "projects/alt/specs/api.md", "---\ntype: spec\ntitle: API\ndescription: d\n---\n\nThe API.\n")
    _write(
        root,
        "projects/alt/specs/pointer.md",
        "---\ntype: spec\ntitle: Ptr\ndescription: d\n---\n\nSee [api](api.md) and [gone](nope.md).\n",
    )
    # an orphan nobody links to
    _write(root, "projects/alt/specs/lonely.md", "---\ntype: spec\ntitle: Lonely\ndescription: d\n---\n\nAlone.\n")

    scan = _scan(root)
    assert {"source": "projects/alt/specs/pointer.md", "target": "projects/alt/specs/nope.md"} in scan["dangling_links"]
    assert "projects/alt/specs/api.md" not in scan["orphans"]  # pointer links it
    assert "projects/alt/specs/lonely.md" in scan["orphans"]
    assert "projects/alt/specs/lonely.md" in scan["dead"]  # orphan + no artifact sources it


async def test_dead_knowledge_exempts_artifact_sources(store: KBStore) -> None:
    root = store.root
    _write(root, "projects/alt/specs/used.md", "---\ntype: spec\ntitle: Used\ndescription: d\n---\n\nUsed by an artifact.\n")
    _write(
        root,
        "projects/alt/artifacts/report.md",
        "---\ntype: artifact\ntitle: R\ndescription: d\nsources:\n  - projects/alt/specs/used.md\n---\n\nBuilt from used.\n",
    )
    scan = _scan(root)
    # used.md has no inbound body links -> orphan, but an artifact sources it -> not dead
    assert "projects/alt/specs/used.md" in scan["orphans"]
    assert "projects/alt/specs/used.md" not in scan["dead"]


async def test_scan_repairs_missing_index_membership(store: KBStore) -> None:
    root = store.root
    # write a concept file directly on disk WITHOUT going through kb_write, so its
    # parent index does not list it
    _write(root, "projects/alt/specs/unindexed.md", "---\ntype: spec\ntitle: Unindexed\ndescription: d\n---\n\nx.\n")
    scan = _scan(root)
    assert "projects/alt/specs/unindexed.md" in scan["index_repairs"]


async def test_run_reconcile_writes_health_report(store: KBStore, settings: Settings, other_clone) -> None:
    # a concept pushed from another clone (tracked, so the server checkout stays clean)
    # but never added to its parent index -> reconcile should repair it
    other_clone.commit_file(
        "projects/alt/specs/lonely.md",
        "---\ntype: spec\ntitle: Lonely\ndescription: d\n---\n\nAlone.\n",
        "add lonely from another PC",
    )
    summary = await run_reconcile(store, semantic_index=None)
    assert summary["report_path"] == "library/reports/brain-health.md"
    assert summary["total_files"] > 0

    report = settings.brain_path / "library/reports/brain-health.md"
    assert report.is_file()
    meta = read_meta(report)
    assert meta["type"] == "report"
    text = report.read_text(encoding="utf-8")
    assert "# Brain Health" in text
    assert "Orphan concepts" in text
    # after reconcile, the directly-written concept is repaired into its index
    specs_idx = (settings.brain_path / "projects/alt/specs/index.md").read_text(encoding="utf-8")
    assert "lonely.md" in specs_idx


async def test_run_reconcile_calls_semantic_reindex(store: KBStore) -> None:
    calls = {}

    class FakeSem:
        def full_reindex(self, root):
            calls["root"] = root
            return {"indexed": 3}

    summary = await run_reconcile(store, semantic_index=FakeSem())
    assert calls["root"] == store.root
    assert summary["report_path"] == "library/reports/brain-health.md"


async def test_reconcile_reports_similar_pairs(store):
    """The nightly sweep surfaces near-duplicate concept pairs in brain-health."""
    from types import SimpleNamespace

    a = "---\ntype: idea\ntitle: Grafana Dashboards\ndescription: Monitoring boards.\n---\n\nSee [ctx](../context.md).\n"
    b = "---\ntype: idea\ntitle: Grafana Boards\ndescription: Boards for monitoring.\n---\n\nSee [ctx](../context.md).\n"
    await store.kb_write("projects/alt/ideas/grafana-dashboards.md", a, "a")
    await store.kb_write("projects/alt/ideas/grafana-boards.md", b, "b")

    def fake_search(query, limit=2):
        if "Grafana" in query:
            other = (
                "projects/alt/ideas/grafana-boards.md"
                if "Dashboards" in query
                else "projects/alt/ideas/grafana-dashboards.md"
            )
            return [{"path": other, "score": 0.93}]
        return []

    fake_idx = SimpleNamespace(
        full_reindex=lambda root: {"indexed": 2, "failed": 0, "skipped": 0},
        search=fake_search,
    )
    from engram_server.reconcile import run_reconcile

    summary = await run_reconcile(store, fake_idx)
    assert summary["similar_pairs"] == 1
    report = (store.root / "library/reports/brain-health.md").read_text(encoding="utf-8")
    assert "Similar pairs" in report
    assert "grafana-boards.md" in report and "grafana-dashboards.md" in report
    assert "0.93" in report


def _seed_workspace(other_clone, now: datetime) -> None:
    """Seed a stale + fresh presence record (with their index) and a stale + fresh open
    handoff via another clone, so the server checkout stays clean after reconcile's pull."""
    stale_p = _iso(now - timedelta(hours=48))
    fresh_p = _iso(now - timedelta(minutes=5))
    other_clone.commit_file(
        "workspace/presence/stale-sess.md",
        "---\ntype: presence\ntitle: Presence A\ndescription: stale session\n"
        f"session: stale-sess\nname: A\nstatus: working\nupdated: {stale_p}\n---\n",
        "add stale presence",
    )
    other_clone.commit_file(
        "workspace/presence/fresh-sess.md",
        "---\ntype: presence\ntitle: Presence B\ndescription: fresh session\n"
        f"session: fresh-sess\nname: B\nstatus: working\nupdated: {fresh_p}\n---\n",
        "add fresh presence",
    )
    other_clone.commit_file(
        "workspace/presence/index.md",
        "# Presence\n\n"
        "* [Presence A](stale-sess.md) - stale session\n"
        "* [Presence B](fresh-sess.md) - fresh session\n",
        "add presence index",
    )
    old_h = _iso(now - timedelta(days=8))
    fresh_h = _iso(now - timedelta(hours=1))
    other_clone.commit_file(
        "workspace/handoffs/20260701T000000-x.md",
        "---\ntype: handoff\ntitle: Handoff from x\ndescription: old\nfrom: x\nto: any\n"
        f"summary: Old handoff work\ncreated: {old_h}\nstatus: open\n---\n\n"
        "# Handoff from x\n\n**To:** any   **Status:** open   **Created:** " + old_h + "\n",
        "add old handoff",
    )
    other_clone.commit_file(
        "workspace/handoffs/20260709T000000-y.md",
        "---\ntype: handoff\ntitle: Handoff from y\ndescription: fresh\nfrom: y\nto: any\n"
        f"summary: Fresh handoff work\ncreated: {fresh_h}\nstatus: open\n---\n\n"
        "# Handoff from y\n\n**To:** any   **Status:** open   **Created:** " + fresh_h + "\n",
        "add fresh handoff",
    )
    other_clone.commit_file(
        "workspace/handoffs/index.md",
        "# Handoffs\n\n"
        "* [Handoff from x](20260701T000000-x.md) - old\n"
        "* [Handoff from y](20260709T000000-y.md) - fresh\n",
        "add handoffs index",
    )


async def test_reconcile_prunes_stale_presence_and_stales_old_handoffs(
    store: KBStore, other_clone
) -> None:
    now = datetime.now(timezone.utc)
    _seed_workspace(other_clone, now)

    summary = await run_reconcile(store, semantic_index=None)
    assert summary["pruned_presence"] == 1
    assert summary["staled_handoffs"] == 1

    root = store.root
    # Stale presence deleted, fresh one kept.
    assert not (root / "workspace/presence/stale-sess.md").is_file()
    assert (root / "workspace/presence/fresh-sess.md").is_file()
    # Its index bullet is gone; the fresh one survives.
    idx = (root / "workspace/presence/index.md").read_text(encoding="utf-8")
    assert "stale-sess.md" not in idx
    assert "fresh-sess.md" in idx

    # Old open handoff flipped to stale (kept as history); fresh one still open.
    old_meta = read_meta(root / "workspace/handoffs/20260701T000000-x.md")
    assert old_meta["status"] == "stale"
    fresh_meta = read_meta(root / "workspace/handoffs/20260709T000000-y.md")
    assert fresh_meta["status"] == "open"

    # The report carries a workspace housekeeping section.
    report = (root / "library/reports/brain-health.md").read_text(encoding="utf-8")
    assert "Workspace housekeeping" in report


async def test_housekeep_never_touches_fresh_records(store: KBStore) -> None:
    """A direct _housekeep call with only fresh records makes no changes."""
    now = datetime.now(timezone.utc)
    root = store.root
    _write(
        root,
        "workspace/presence/live.md",
        "---\ntype: presence\ntitle: Live\ndescription: d\nsession: live\n"
        f"status: working\nupdated: {_iso(now - timedelta(minutes=2))}\n---\n",
    )
    _write(
        root,
        "workspace/handoffs/20260709T120000-z.md",
        "---\ntype: handoff\ntitle: Handoff from z\ndescription: d\nfrom: z\nto: any\n"
        f"summary: recent\ncreated: {_iso(now - timedelta(hours=2))}\nstatus: open\n---\n\nbody\n",
    )
    changed, hk = _housekeep(root, now, 24.0, 7.0)
    assert changed == []
    assert hk == {"pruned_presence": [], "staled_handoffs": []}
    assert (root / "workspace/presence/live.md").is_file()


async def test_outputs_exempt_from_orphans_and_dead(store):
    """Artifacts/reports are outputs — provenance measures them, not inbound links."""
    art = (
        "---\ntype: artifact\ntitle: Some Brief\ndescription: d\n"
        "sources:\n  - projects/alt/context.md\n---\n\nBody.\n"
    )
    await store.kb_write("projects/alt/artifacts/2026-07-some-brief.md", art, "add artifact")
    orphan = "---\ntype: idea\ntitle: Truly Alone\ndescription: d\n---\n\nNo links at all here.\n"
    await store.kb_write("projects/alt/ideas/truly-alone.md", orphan, "add orphan")

    from engram_server.reconcile import run_reconcile

    summary = await run_reconcile(store, None)
    report = (store.root / "library/reports/brain-health.md").read_text(encoding="utf-8")
    assert "some-brief.md" not in report          # artifact exempt
    assert "truly-alone.md" in report             # real orphan still flagged
