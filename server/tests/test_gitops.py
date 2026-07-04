"""GitRepo: clone/config, pull --rebase, conflict abort, commit+push, push-race retry."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engram_server.errors import GitError
from engram_server.gitops import GitRepo

DESC_LINE = "description: TODO one-line project description."
CONTEXT = "projects/alt/context.md"


def _remote_head(remote_repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(remote_repo), "rev-parse", "main"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(repo: GitRepo, relpath: str, content: str) -> None:
    target = repo.path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def test_clone_creates_checkout_and_enforces_config(gitrepo: GitRepo) -> None:
    assert (gitrepo.path / ".git").is_dir()
    assert (gitrepo.path / "index.md").is_file()
    assert (gitrepo.path / CONTEXT).is_file()
    for key, want in (
        ("core.autocrlf", "false"),
        ("core.longpaths", "true"),
        ("pull.rebase", "true"),
    ):
        assert gitrepo.run("config", "--get", key).strip() == want
    assert gitrepo.is_dirty() == []


def test_ensure_clone_is_idempotent(gitrepo: GitRepo) -> None:
    sha = gitrepo.head_sha()
    gitrepo.ensure_clone()  # checkout already exists — must only re-enforce config
    assert gitrepo.head_sha() == sha


def test_pull_absorbs_remote_commit(gitrepo: GitRepo, other_clone) -> None:
    remote_sha = other_clone.commit_file(
        "self/from-other-pc.md",
        "---\ntype: reference\ntitle: From other PC\n---\nhello\n",
        "feat: note from another PC",
    )
    gitrepo.pull_rebase()
    assert (gitrepo.path / "self" / "from-other-pc.md").is_file()
    assert gitrepo.head_sha() == remote_sha


def test_conflicting_pull_raises_and_leaves_no_rebase(gitrepo: GitRepo, other_clone) -> None:
    original = (gitrepo.path / CONTEXT).read_text(encoding="utf-8")
    assert DESC_LINE in original
    other_clone.commit_file(
        CONTEXT, original.replace(DESC_LINE, "description: remote change."), "remote edit"
    )
    _write(gitrepo, CONTEXT, original.replace(DESC_LINE, "description: local change."))
    gitrepo.run("add", "-A", "--", CONTEXT)
    gitrepo.run("commit", "-m", "local edit")
    local_sha = gitrepo.head_sha()

    with pytest.raises(GitError, match="out of sync"):
        gitrepo.pull_rebase()

    assert gitrepo.rebase_in_progress() is False
    assert gitrepo.is_dirty() == []
    assert gitrepo.head_sha() == local_sha  # abort restored the local commit


def test_commit_and_push_advances_remote(gitrepo: GitRepo, remote_repo: Path) -> None:
    before = _remote_head(remote_repo)
    _write(
        gitrepo,
        "library/runbooks/test-runbook.md",
        "---\ntype: runbook\ntitle: Test\n---\nsteps\n",
    )
    sha, pushed = gitrepo.commit_and_push(["library/runbooks/test-runbook.md"], "feat: add runbook")
    assert pushed is True
    assert sha == gitrepo.head_sha()
    assert _remote_head(remote_repo) == sha != before
    # _env() must override the suite's seed-author env vars with the configured author.
    assert gitrepo.run("log", "-1", "--format=%an <%ae>").strip() == "helix-bot <helix@metalfinger.xyz>"


def test_commit_and_push_noop_when_nothing_staged(gitrepo: GitRepo, remote_repo: Path) -> None:
    before = gitrepo.head_sha()
    sha, pushed = gitrepo.commit_and_push(["index.md"], "chore: nothing changed")
    assert (sha, pushed) == (before, True)
    assert _remote_head(remote_repo) == before


def test_push_race_rebases_and_retries(gitrepo: GitRepo, other_clone, remote_repo: Path) -> None:
    # Remote moves after our clone -> first push is rejected -> pull --rebase -> retry.
    other_clone.commit_file(
        "self/remote-note.md", "---\ntype: reference\n---\nremote\n", "feat: remote note"
    )
    _write(gitrepo, "self/local-note.md", "---\ntype: reference\n---\nlocal\n")
    seam_calls: list[int] = []
    gitrepo._after_pull = lambda: seam_calls.append(1)

    sha, pushed = gitrepo.commit_and_push(["self/local-note.md"], "feat: local note")

    assert pushed is True
    assert seam_calls == [1]  # proves the first push failed and the retry path ran
    assert _remote_head(remote_repo) == sha
    assert (gitrepo.path / "self" / "remote-note.md").is_file()  # rebase absorbed remote work


def test_push_race_conflict_preserves_local_commit(gitrepo: GitRepo, other_clone) -> None:
    original = (gitrepo.path / CONTEXT).read_text(encoding="utf-8")
    other_clone.commit_file(
        CONTEXT, original.replace(DESC_LINE, "description: remote change."), "remote edit"
    )
    _write(gitrepo, CONTEXT, original.replace(DESC_LINE, "description: local change."))

    with pytest.raises(GitError, match="preserved locally"):
        gitrepo.commit_and_push([CONTEXT], "feat: local edit")

    assert gitrepo.rebase_in_progress() is False
    assert gitrepo.is_dirty() == []
    assert gitrepo.run("log", "-1", "--format=%s").strip() == "feat: local edit"
