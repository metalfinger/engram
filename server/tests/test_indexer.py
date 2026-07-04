"""Pure filesystem tests for engram_server.indexer (tmp_path only, no git)."""

from __future__ import annotations

from pathlib import Path

from engram_server.indexer import ensure_indexed


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_double_add_is_idempotent(tmp_path: Path) -> None:
    idx = tmp_path / "projects/alt/decisions/index.md"
    write(idx, "# decisions\n\nNothing here yet.\n")

    first = ensure_indexed(tmp_path, "projects/alt/decisions/2026-07-choice.md", "Choice", "Why we chose it.")
    assert first == ["projects/alt/decisions/index.md"]

    second = ensure_indexed(tmp_path, "projects/alt/decisions/2026-07-choice.md", "Choice", "Why we chose it.")
    assert second == []

    text = read(idx)
    assert text.count("2026-07-choice.md") == 1
    assert "* [Choice](2026-07-choice.md) - Why we chose it." in text


def test_placeholder_prose_replaced_not_appended(tmp_path: Path) -> None:
    idx = tmp_path / "library/runbooks/index.md"
    write(idx, "# runbooks\n\nNothing here yet.\n")

    ensure_indexed(tmp_path, "library/runbooks/tunnel-restart.md", "Tunnel Restart", "Zero-downtime restart.")

    text = read(idx)
    assert "Nothing here yet." not in text
    assert text == "# runbooks\n\n* [Tunnel Restart](tunnel-restart.md) - Zero-downtime restart.\n"


def test_append_preserves_heading_groups(tmp_path: Path) -> None:
    original = (
        "# alt\n\n"
        "## Core\n\n"
        "* [Context](context.md) - Living state.\n\n"
        "## Notes\n\n"
        "* [Standup](standup.md) - Weekly notes.\n"
    )
    idx = tmp_path / "projects/alt/index.md"
    write(idx, original)

    ensure_indexed(tmp_path, "projects/alt/roadmap.md", "Roadmap", "Q3 plan.")

    text = read(idx)
    assert text.startswith(original.rstrip("\n"))
    assert "## Core" in text and "## Notes" in text
    assert text.endswith("* [Standup](standup.md) - Weekly notes.\n* [Roadmap](roadmap.md) - Q3 plan.\n")
    assert not text.endswith("\n\n")


def test_new_dir_chain_creates_parent_indexes_up_to_existing(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Brain\n\n* [Projects](projects/index.md) - Active work.\n")
    write(tmp_path / "projects/index.md", "# Projects\n\n* [Alt Inc](alt/index.md) - Alt Inc work.\n")
    write(tmp_path / "projects/alt/index.md", "# alt\n\n* [Context](context.md) - Living state.\n")

    modified = ensure_indexed(
        tmp_path, "projects/alt/research/papers/attention.md", "Attention", "Survey of attention papers."
    )
    assert modified == [
        "projects/alt/research/papers/index.md",
        "projects/alt/research/index.md",
        "projects/alt/index.md",
    ]

    papers = read(tmp_path / "projects/alt/research/papers/index.md")
    assert papers == "# Papers\n\n* [Attention](attention.md) - Survey of attention papers.\n"

    research = read(tmp_path / "projects/alt/research/index.md")
    assert research == "# Research\n\n* [Papers](papers/index.md) - Survey of attention papers.\n"

    alt = read(tmp_path / "projects/alt/index.md")
    assert alt.endswith("* [Research](research/index.md) - Survey of attention papers.\n")

    # Ancestors above the first existing index are untouched.
    assert read(tmp_path / "projects/index.md") == "# Projects\n\n* [Alt Inc](alt/index.md) - Alt Inc work.\n"
    assert read(tmp_path / "index.md") == "# Brain\n\n* [Projects](projects/index.md) - Active work.\n"


def test_chain_stops_when_ancestor_already_linked(tmp_path: Path) -> None:
    write(tmp_path / "projects/alt/index.md", "# alt\n\n* [Research](research/index.md) - Research area.\n")

    modified = ensure_indexed(tmp_path, "projects/alt/research/deep.md", "Deep", "A note.")
    assert modified == ["projects/alt/research/index.md"]
    assert read(tmp_path / "projects/alt/index.md").count("research/index.md") == 1


def test_root_index_created_without_frontmatter(tmp_path: Path) -> None:
    modified = ensure_indexed(tmp_path, "deccan-transcon/seo-audit.md", "SEO Audit", "Findings.")
    assert modified == ["deccan-transcon/index.md", "index.md"]

    sub = read(tmp_path / "deccan-transcon/index.md")
    root = read(tmp_path / "index.md")
    assert sub.splitlines()[0] == "# Deccan Transcon"  # kebab -> Title Case
    for text in (sub, root):
        assert "---" not in text
        assert text.startswith("# ")
    assert "* [Deccan Transcon](deccan-transcon/index.md) - Findings.\n" in root


def test_empty_description_omits_dash(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Brain\n\nNothing here yet.\n")

    ensure_indexed(tmp_path, "notes/idea.md", "Idea", "")

    notes = read(tmp_path / "notes/index.md")
    root = read(tmp_path / "index.md")
    assert "* [Idea](idea.md)\n" in notes
    assert "* [Notes](notes/index.md)\n" in root
    assert " - \n" not in notes and " - \n" not in root
