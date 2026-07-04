"""Write serialization, conflict handling, dirty-guard, and stale reads."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.kbstore import KBStore

CONTEXT = "projects/alt/context.md"
DESC_LINE = "description: TODO one-line project description."


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


def _remote_subjects(remote_repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(remote_repo), "log", "--format=%s", "main"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


async def test_conflicting_edit_fails_with_teaching_error_and_clean_checkout(
    store: KBStore, settings: Settings, other_clone
) -> None:
    abs_ctx = settings.brain_path / CONTEXT
    original = abs_ctx.read_text(encoding="utf-8")
    assert DESC_LINE in original

    # A previously committed-but-unpushed local edit (push backlog on the server)...
    local = original.replace(DESC_LINE, "description: local backlog edit.")
    with open(abs_ctx, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(local)
    store.repo.run("add", "-A", "--", CONTEXT)
    store.repo.run("commit", "-m", "local backlog")

    # ...collides with a concurrent edit of the same line from another PC.
    other_clone.commit_file(
        CONTEXT, original.replace(DESC_LINE, "description: remote edit."), "remote edit"
    )

    attempted = original.replace(DESC_LINE, "description: tool-write edit.")
    with pytest.raises(KBError, match="out of sync"):
        await store.kb_write(CONTEXT, attempted, "server edit")

    # Checkout left clean: no rebase in progress, not dirty, nothing was written.
    assert store.repo.rebase_in_progress() is False
    assert store.repo.is_dirty() == []
    assert abs_ctx.read_text(encoding="utf-8") == local


async def test_concurrent_writes_both_reach_remote(
    store: KBStore, remote_repo: Path, other_clone
) -> None:
    res_a, res_b = await asyncio.gather(
        store.kb_write(
            "projects/alt/decisions/2026-07-a.md",
            "---\ntype: decision\ndescription: A.\n---\nA.\n",
            "decision a",
        ),
        store.kb_write(
            "projects/alt/decisions/2026-07-b.md",
            "---\ntype: decision\ndescription: B.\n---\nB.\n",
            "decision b",
        ),
    )
    assert res_a["pushed"] is True
    assert res_b["pushed"] is True
    assert res_a["sha"] != res_b["sha"]

    subjects = _remote_subjects(remote_repo)
    assert "kb: decision a" in subjects
    assert "kb: decision b" in subjects
    assert _remote_head(remote_repo) in (res_a["sha"], res_b["sha"])

    # Both files (and their index bullets) visible from another clone.
    other_clone.git("pull", "origin", "main")
    assert (other_clone.path / "projects/alt/decisions/2026-07-a.md").is_file()
    assert (other_clone.path / "projects/alt/decisions/2026-07-b.md").is_file()
    idx = (other_clone.path / "projects/alt/decisions/index.md").read_text(encoding="utf-8")
    assert "2026-07-a.md" in idx
    assert "2026-07-b.md" in idx


async def test_dirty_checkout_blocks_writes(store: KBStore, settings: Settings) -> None:
    stray = settings.brain_path / "stray.md"
    stray.write_text("junk\n", encoding="utf-8")
    with pytest.raises(KBError, match="stray.md"):
        await store.kb_write(
            "library/snippets/x.md",
            "---\ntype: snippet\ndescription: X.\n---\nX.\n",
            "attempt",
        )
    assert not (settings.brain_path / "library/snippets/x.md").exists()


async def test_reads_serve_stale_when_remote_unreachable(
    settings: Settings, tmp_path: Path
) -> None:
    store = KBStore(settings)
    await store.start()
    # GitHub is "down" — every pull now fails.
    store.repo.run("remote", "set-url", "origin", (tmp_path / "gone.git").as_uri())
    rows = await store.kb_projects()  # _refresh swallows the GitError
    assert [r["id"] for r in rows][-1] == "metalfinger"
    res = await store.kb_read(CONTEXT)
    assert res["meta"]["type"] == "project"
