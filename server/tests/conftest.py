"""Shared fixtures: a file:// bare remote seeded from the brain skeleton — zero network."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from engram_server.config import Settings
from engram_server.gitops import GitRepo

SKELETON = Path(__file__).resolve().parents[2] / "brain-skeleton" / "brain"


def _git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` for fixture plumbing; assert success."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


@pytest.fixture(scope="session", autouse=True)
def _git_isolation(tmp_path_factory: pytest.TempPathFactory):
    """Isolate every git invocation in the suite from host/global config.

    GIT_CONFIG_GLOBAL points at an empty temp file (Windows has no /dev/null
    file path git accepts); GIT_CONFIG_NOSYSTEM kills the system config; the
    author/committer vars let fixture seed commits work without any user.name.
    """
    mp = pytest.MonkeyPatch()
    empty_cfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    empty_cfg.write_text("", encoding="utf-8")
    mp.setenv("GIT_CONFIG_GLOBAL", str(empty_cfg))
    mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
    mp.setenv("GIT_TERMINAL_PROMPT", "0")
    mp.setenv("GIT_AUTHOR_NAME", "test-seed")
    mp.setenv("GIT_AUTHOR_EMAIL", "seed@test.invalid")
    mp.setenv("GIT_COMMITTER_NAME", "test-seed")
    mp.setenv("GIT_COMMITTER_EMAIL", "seed@test.invalid")
    yield
    mp.undo()


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    """Bare repo on disk seeded with the full brain skeleton, branch main."""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    proc = subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(
        ["git", "init", "-b", "main", str(seed)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    shutil.copytree(SKELETON, seed, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed: brain skeleton")
    _git(seed, "remote", "add", "origin", bare.as_uri())
    _git(seed, "push", "origin", "main")
    return bare


@pytest.fixture
def settings(tmp_path: Path, remote_repo: Path) -> Settings:
    return Settings(
        _env_file=None,
        brain_path=tmp_path / "brain",
        brain_remote=remote_repo.as_uri(),
        brain_branch="main",
        deploy_key_path=tmp_path / "no-such-key",
        pull_ttl=0.0,
    )


@pytest.fixture
def gitrepo(settings: Settings) -> GitRepo:
    repo = GitRepo(
        path=settings.brain_path,
        remote=settings.brain_remote,
        branch=settings.brain_branch,
        ssh_key=settings.deploy_key_path,
        author=(settings.git_author_name, settings.git_author_email),
        timeout=settings.git_timeout,
    )
    repo.ensure_clone()
    return repo


class OtherClone:
    """A second working clone — simulates Hiren editing from another PC."""

    def __init__(self, path: Path, branch: str) -> None:
        self.path = path
        self.branch = branch

    def git(self, *args: str) -> str:
        return _git(self.path, *args)

    def commit_file(self, relpath: str, content: str, msg: str) -> str:
        """Write ``relpath``, commit, push. Returns the new commit sha."""
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        self.git("add", "-A", "--", relpath)
        self.git("commit", "-m", msg)
        self.git("push", "origin", self.branch)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def other_clone(remote_repo: Path, tmp_path: Path) -> OtherClone:
    path = tmp_path / "other"
    proc = subprocess.run(
        ["git", "clone", remote_repo.as_uri(), str(path)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return OtherClone(path, "main")
