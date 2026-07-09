"""Tests for engram_server.search — pure, tmp_path corpus, no conftest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from engram_server.errors import KBError
from engram_server.search import search


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _concept(type_: str, title: str, description: str, body: str, **extra: str) -> str:
    fields = "\n".join(f"{k}: {v}" for k, v in extra.items())
    fields = f"{fields}\n" if fields else ""
    return (
        f"---\ntype: {type_}\ntitle: {title}\ndescription: {description}\n"
        f"timestamp: 2026-07-01T00:00:00Z\n{fields}---\n\n{body}\n"
    )


def test_title_hit_outranks_body_hit_and_matched_heading(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "projects/alt/decisions/2026-07-qdrant.md",
        _concept("decision", "Qdrant chosen", "Vector store pick", "Decision recorded in chat."),
    )
    _write(
        tmp_path,
        "library/runbooks/search-notes.md",
        _concept(
            "runbook",
            "Search notes",
            "Engine survey",
            "# Search notes\n\n## Engine comparison\n\nWe looked at qdrant briefly.",
        ),
    )
    results = search(tmp_path, "qdrant")
    assert [r["path"] for r in results] == [
        "projects/alt/decisions/2026-07-qdrant.md",
        "library/runbooks/search-notes.md",
    ]
    assert results[0]["score"] > results[1]["score"]
    assert results[0]["title"] == "Qdrant chosen"
    assert results[0]["description"] == "Vector store pick"
    assert results[0]["matched_heading"] is None  # no body hit
    assert results[1]["matched_heading"] == "Engine comparison"


def test_project_filter_frontmatter_path_and_metalfinger(tmp_path: Path) -> None:
    _write(tmp_path, "projects/alt/specs/api.md", _concept("spec", "API", "Alt API", "grafana dashboard"))
    _write(tmp_path, "projects/hyprlocl/specs/site.md", _concept("spec", "Site", "Hyprlocl", "grafana panel"))
    _write(
        tmp_path,
        "library/runbooks/dash.md",
        _concept("runbook", "Dash setup", "Shared runbook", "grafana install", project="alt"),
    )
    _write(tmp_path, "metalfinger/videos/helix.md", _concept("video", "Helix video", "Channel", "grafana b-roll"))

    alt = {r["path"] for r in search(tmp_path, "grafana", project="alt")}
    assert alt == {"projects/alt/specs/api.md", "library/runbooks/dash.md"}

    hyprlocl = {r["path"] for r in search(tmp_path, "grafana", project="hyprlocl")}
    assert hyprlocl == {"projects/hyprlocl/specs/site.md"}

    # special case: metalfinger lives at repo root, not under projects/
    mf = {r["path"] for r in search(tmp_path, "grafana", project="metalfinger")}
    assert mf == {"metalfinger/videos/helix.md"}


def test_type_filter(tmp_path: Path) -> None:
    _write(tmp_path, "projects/alt/decisions/2026-07-x.md", _concept("decision", "X", "d", "grafana"))
    _write(tmp_path, "library/runbooks/dash.md", _concept("runbook", "Dash", "d", "grafana"))
    only_runbooks = search(tmp_path, "grafana", type_="runbook")
    assert [r["path"] for r in only_runbooks] == ["library/runbooks/dash.md"]


def test_phrase_bonus(tmp_path: Path) -> None:
    _write(tmp_path, "library/snippets/p.md", _concept("snippet", "P note", "a", "The voice cloning pipeline is ready."))
    _write(tmp_path, "library/snippets/q.md", _concept("snippet", "Q note", "b", "We tried cloning it; the voice was off."))
    results = search(tmp_path, "voice cloning")
    assert [r["path"] for r in results] == ["library/snippets/p.md", "library/snippets/q.md"]
    # p: 2 body hits + phrase bonus 4 = 6; q: 2 body hits; denom = 2*9+4 = 22
    assert results[0]["score"] == pytest.approx(6 / 22, abs=1e-3)
    assert results[1]["score"] == pytest.approx(2 / 22, abs=1e-3)


def test_index_excluded_log_included_git_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "projects/alt/index.md", "# Alt\n\n* [Zebrafish](zebrafish.md) - zebrafish study\n")
    _write(tmp_path, "projects/alt/log.md", "# Alt log\n\n## 2026-07-01 — kickoff\n\nZebrafish tank ordered.\n")
    _write(tmp_path, ".git/notes.md", "zebrafish inside git dir\n")
    results = search(tmp_path, "zebrafish")
    assert [r["path"] for r in results] == ["projects/alt/log.md"]
    assert results[0]["title"] == "log"  # no frontmatter -> filename stem
    assert results[0]["matched_heading"] == "2026-07-01 — kickoff"


def test_threads_and_workspace_excluded_from_corpus(tmp_path: Path) -> None:
    """Coordination ephemera (threads/, workspace/) is not knowledge — never returned."""
    _write(tmp_path, "projects/alt/specs/api.md", _concept("spec", "API", "d", "kumquat design"))
    _write(tmp_path, "threads/deploy/thread.md", _concept("thread", "Deploy room", "d", "kumquat chatter"))
    _write(tmp_path, "workspace/presence/pc1.md", _concept("presence", "PC1", "d", "kumquat working_on"))
    _write(tmp_path, "workspace/handoffs/20260709-x.md", _concept("handoff", "Handoff", "d", "kumquat handoff"))
    paths = {r["path"] for r in search(tmp_path, "kumquat")}
    assert paths == {"projects/alt/specs/api.md"}


def test_limit_clamped_1_to_25(tmp_path: Path) -> None:
    for i in range(30):
        _write(tmp_path, f"library/snippets/s{i:02d}.md", _concept("snippet", f"S{i}", "d", "kumquat everywhere"))
    assert len(search(tmp_path, "kumquat", limit=100)) == 25
    assert len(search(tmp_path, "kumquat", limit=0)) == 1
    assert len(search(tmp_path, "kumquat", limit=-7)) == 1
    assert len(search(tmp_path, "kumquat")) == 8  # default


def test_empty_query_raises(tmp_path: Path) -> None:
    _write(tmp_path, "library/snippets/a.md", _concept("snippet", "A", "d", "body"))
    with pytest.raises(KBError):
        search(tmp_path, "")
    with pytest.raises(KBError):
        search(tmp_path, "   \t ")
    with pytest.raises(KBError):
        search(tmp_path, "!!! ...")  # punctuation-only: no tokens


def test_tiebreak_newer_timestamp_first(tmp_path: Path) -> None:
    old = "---\ntype: idea\ntitle: Old idea\ndescription: d\ntimestamp: 2026-01-01T00:00:00Z\n---\n\nkumquat\n"
    new = "---\ntype: idea\ntitle: New idea\ndescription: d\ntimestamp: 2026-06-01T00:00:00Z\n---\n\nkumquat\n"
    _write(tmp_path, "library/snippets/aaa-old.md", old)
    _write(tmp_path, "library/snippets/zzz-new.md", new)
    results = search(tmp_path, "kumquat")
    assert results[0]["score"] == results[1]["score"]
    assert [r["path"] for r in results] == ["library/snippets/zzz-new.md", "library/snippets/aaa-old.md"]
