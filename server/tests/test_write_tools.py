"""kb_write (validation, index maintenance, push) and kb_append_log."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore

VALID = "---\ntype: idea\ndescription: Valid concept.\n---\nBody.\n"


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _remote_head(remote_repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(remote_repo), "rev-parse", "main"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _pin_today(monkeypatch: pytest.MonkeyPatch, iso: str) -> None:
    """Freeze kb_append_log's notion of 'today' so heading placement is deterministic
    regardless of the wall clock (and of the skeleton's own 2026-07-04 seed heading)."""
    fixed = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    monkeypatch.setattr("engram_server.kbstore._utcnow", lambda: fixed)


async def test_write_autofills_and_pushes(
    store: KBStore, settings: Settings, remote_repo: Path
) -> None:
    before = _remote_head(remote_repo)
    content = "---\ntype: decision\n---\n# Choice\n\nWe chose X.\n"
    res = await store.kb_write(
        "projects/alt/decisions/2026-07-choose-x.md",
        content,
        "record decision",
        description="Why we chose X.",
    )
    assert res["created"] is True
    assert res["no_change"] is False
    assert res["pushed"] is True
    assert res["sha"] != before
    assert _remote_head(remote_repo) == res["sha"]  # remote HEAD advanced

    meta = read_meta(settings.brain_path / "projects/alt/decisions/2026-07-choose-x.md")
    assert meta["type"] == "decision"
    assert meta["title"] == "2026 07 Choose X"  # filename-derived, H1 never consulted
    assert meta["description"] == "Why we chose X."
    assert meta["timestamp"]  # auto-filled now-UTC
    assert store.repo.run("log", "-1", "--format=%s").strip() == "kb: record decision"


async def test_update_gains_exactly_one_bullet_across_two_writes(
    store: KBStore, settings: Settings, remote_repo: Path
) -> None:
    path = "projects/alt/decisions/2026-07-choose-x.md"
    r1 = await store.kb_write(
        path, "---\ntype: decision\ndescription: Why X.\n---\nFirst.\n", "record"
    )
    r2 = await store.kb_write(
        path, "---\ntype: decision\ndescription: Why X.\n---\nSecond, revised.\n", "revise"
    )
    assert r1["created"] is True
    assert r1["indexes_updated"] == ["projects/alt/decisions/index.md"]
    assert r2["created"] is False
    assert r2["indexes_updated"] == []
    idx = (settings.brain_path / "projects/alt/decisions/index.md").read_text(encoding="utf-8")
    assert idx.count("2026-07-choose-x.md") == 1  # exactly one bullet
    assert "Nothing here yet." not in idx  # placeholder prose replaced
    assert r2["sha"] != r1["sha"]
    assert _remote_head(remote_repo) == r2["sha"]


async def test_write_unchanged_content_is_a_noop(store: KBStore, remote_repo: Path) -> None:
    path = "library/runbooks/deploy.md"
    full = (
        "---\n"
        "type: runbook\n"
        "title: Deploy\n"
        "description: How to deploy.\n"
        "timestamp: 2026-07-04T10:00:00Z\n"
        "---\n"
        "Steps.\n"
    )
    r1 = await store.kb_write(path, full, "add runbook")
    r2 = await store.kb_write(path, full, "same again")
    assert r2["no_change"] is True
    assert r2["created"] is False
    assert r2["sha"] == r1["sha"]  # HEAD untouched
    assert r2["indexes_updated"] == []
    assert _remote_head(remote_repo) == r1["sha"]


@pytest.mark.parametrize(
    ("path", "content", "match"),
    [
        ("../escape.md", VALID, "escape"),
        ("projects/alt/index.md", VALID, "maintained automatically"),
        ("projects/alt/log.md", VALID, "kb_append_log"),
        ("projects/alt/messages/2026-07-04-note.md", VALID, "kb_leave_message"),
        ("projects/alt/notes.txt", VALID, r"\.md"),
        (
            "projects/alt/no-type.md",
            "---\ntitle: X\ndescription: D.\n---\nBody.\n",
            "non-empty 'type'",
        ),
        ("projects/alt/no-desc.md", "---\ntype: idea\n---\nBody.\n", "description"),
    ],
)
async def test_write_rejections_teach(
    store: KBStore, settings: Settings, path: str, content: str, match: str
) -> None:
    with pytest.raises(KBError, match=match):
        await store.kb_write(path, content, "attempt")
    assert store.repo.is_dirty() == []  # nothing was written


async def test_write_context_md_is_allowed(store: KBStore, settings: Settings) -> None:
    original = (settings.brain_path / "projects/alt/context.md").read_text(encoding="utf-8")
    updated = original.replace("# About\n\nTODO", "# About\n\nAlt Inc paid trial.")
    res = await store.kb_write("projects/alt/context.md", updated, "update context")
    assert res["created"] is False
    assert res["pushed"] is True
    assert "Alt Inc paid trial." in (
        settings.brain_path / "projects/alt/context.md"
    ).read_text(encoding="utf-8")


async def test_write_replaces_placeholder_and_chains_new_dirs(
    store: KBStore, settings: Settings
) -> None:
    # specs/index.md is a prose placeholder -> first bullet replaces it
    res = await store.kb_write(
        "projects/alt/specs/api.md",
        "---\ntype: spec\ndescription: The API spec.\n---\nSpec body.\n",
        "add api spec",
    )
    assert res["indexes_updated"] == ["projects/alt/specs/index.md"]
    specs_idx = (settings.brain_path / "projects/alt/specs/index.md").read_text(encoding="utf-8")
    assert "Nothing here yet." not in specs_idx
    assert "* [Api](api.md) - The API spec." in specs_idx

    # brand-new nested dir -> index chain created upward, deepest first
    res2 = await store.kb_write(
        "projects/alt/research/notes/deep-dive.md",
        "---\ntype: reference\ndescription: Deep dive notes.\n---\nStuff.\n",
        "add deep dive",
    )
    assert res2["indexes_updated"] == [
        "projects/alt/research/notes/index.md",
        "projects/alt/research/index.md",
        "projects/alt/index.md",
    ]
    notes_idx = (settings.brain_path / "projects/alt/research/notes/index.md").read_text(
        encoding="utf-8"
    )
    assert "* [Deep Dive](deep-dive.md) - Deep dive notes." in notes_idx
    research_idx = (settings.brain_path / "projects/alt/research/index.md").read_text(
        encoding="utf-8"
    )
    assert "](notes/index.md)" in research_idx
    alt_idx = (settings.brain_path / "projects/alt/index.md").read_text(encoding="utf-8")
    assert "](research/index.md)" in alt_idx


async def test_append_log_prepends_newest_first_and_keeps_history(
    store: KBStore, settings: Settings, remote_repo: Path
) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r1 = await store.kb_append_log("alt", "Shipped the API spec.\nDetails about the ship.")
    r2 = await store.kb_append_log("alt", "## 2099-01-01 — Custom heading\n\nCustom body.")
    assert r1 == {
        "ok": True,
        "path": "projects/alt/log.md",
        "sha": r1["sha"],
        "date": today,
        "pushed": True,
    }

    text = (settings.brain_path / "projects/alt/log.md").read_text(encoding="utf-8")
    # OKF-strict: entries render as bullets, not decorated headings — a '## date' in the
    # entry is only a title source, never a heading in the file.
    assert "## 2099-01-01" not in text
    assert "* **Custom heading** — Custom body." in text
    assert "* **Shipped the API spec.** — Details about the ship." in text
    # newest bullet first, both sitting above the seeded history (never rewritten)
    i_custom = text.index("* **Custom heading**")
    i_shipped = text.index("* **Shipped the API spec.**")
    i_history = text.index("Skeleton created from the Engram handoff.")  # history intact
    assert i_custom < i_shipped < i_history

    assert _remote_head(remote_repo) == r2["sha"]
    assert store.repo.run("log", "-1", "--format=%s").strip() == f"log: alt {today}"


async def test_append_log_new_day_creates_bare_date_heading(
    store: KBStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_today(monkeypatch, "2026-07-10")
    res = await store.kb_append_log("alt", "Shipped the API spec.\nDetails about the ship.")
    assert res["date"] == "2026-07-10"

    text = (settings.brain_path / "projects/alt/log.md").read_text(encoding="utf-8")
    # a bare ISO date heading (no ' — decoration') is created right after the H1
    assert text.startswith(
        "# Log — alt\n\n## 2026-07-10\n* **Shipped the API spec.** — Details about the ship.\n"
    )
    # the older seed day is left exactly as it was, below the new one
    assert "## 2026-07-04 — Bundle initialized" in text
    assert "Skeleton created from the Engram handoff." in text
    assert store.repo.run("log", "-1", "--format=%s").strip() == "log: alt 2026-07-10"


async def test_append_log_same_day_shares_heading_and_indents_multiline(
    store: KBStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_today(monkeypatch, "2026-07-10")
    await store.kb_append_log("alt", "First thing.")
    await store.kb_append_log("alt", "Second thing.\nWith a detail line.\nAnd another.")

    text = (settings.brain_path / "projects/alt/log.md").read_text(encoding="utf-8")
    # a same-day second append lands under the one existing heading, not a new one
    assert text.count("## 2026-07-10") == 1
    # newest bullet first within the day
    assert text.index("* **Second thing.**") < text.index("* **First thing.**")
    # a multi-line entry: first body line inline after ' — ', the rest indented two spaces
    assert (
        "* **Second thing.** — With a detail line.\n  And another.\n* **First thing.**" in text
    )


async def test_append_log_unknown_project(store: KBStore) -> None:
    with pytest.raises(KBError, match="kb_projects"):
        await store.kb_append_log("ghost-project", "entry")


async def test_write_warns_when_concept_has_no_links(store):
    linkless = "---\ntype: idea\ndescription: An unlinked thought.\n---\n\nNo references here.\n"
    r = await store.kb_write("projects/engram/ideas/loner.md", linkless, "add loner idea")
    assert any("links to nothing" in w for w in r["warnings"])

    linked = (
        "---\ntype: idea\ndescription: A linked thought.\n---\n\n"
        "Relates to [context](../context.md).\n"
    )
    r2 = await store.kb_write("projects/engram/ideas/social.md", linked, "add social idea")
    assert not any("links to nothing" in w for w in r2["warnings"])


async def test_rename_project_moves_tree_and_rewrites_links(store):
    # a concept in another tree linking into the project (deep form)
    outside = (
        "---\ntype: runbook\ndescription: Links into engram.\n---\n\n"
        "See [the spec](../projects/alt/context.md).\n"
    )
    await store.kb_write("library/runbooks/pointer.md", outside, "add pointer")

    r = await store.kb_rename_project("alt", "alt-core")
    assert r["old"] == "alt" and r["new"] == "alt-core"
    assert r["links_rewritten"] >= 1

    root = store.root
    assert not (root / "projects/alt").exists()
    assert (root / "projects/alt-core/context.md").exists()
    # deep link rewritten
    assert "projects/alt-core/context.md" in (root / "library/runbooks/pointer.md").read_text(
        encoding="utf-8"
    )
    # projects/index.md bullet rewritten
    assert "alt-core/" in (root / "projects/index.md").read_text(encoding="utf-8")
    # old id now unknown, new id loads
    import pytest as _pytest
    from engram_server.errors import KBError as _KBError

    with _pytest.raises(_KBError):
        await store.kb_load("alt")
    load = await store.kb_load("alt-core")
    assert load["project"] == "alt-core"


async def test_rename_project_rejects_metalfinger_and_collisions(store):
    import pytest as _pytest
    from engram_server.errors import KBError as _KBError

    with _pytest.raises(_KBError):
        await store.kb_rename_project("metalfinger", "brand")
    with _pytest.raises(_KBError):
        await store.kb_rename_project("nonexistent", "whatever")
