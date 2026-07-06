"""Nightly reconcile + brain-health report."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore
from engram_server.reconcile import _scan, run_reconcile


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
