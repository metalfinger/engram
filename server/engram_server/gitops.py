"""Git plumbing for the brain checkout.

Every operation is a synchronous ``subprocess.run`` (asyncio subprocesses break
under the Windows selector event loop); async callers wrap calls in
``anyio.to_thread.run_sync``. Invariant: the checkout is always left clean —
never mid-rebase — so a failed write can simply be retried.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from engram_server.errors import GitError

_CONFLICT_MSG = (
    "KB is out of sync: a concurrent edit conflicts with this change. "
    "The write was NOT applied. Re-read the file and retry."
)

_LOCAL_CONFIG = (
    ("core.autocrlf", "false"),
    ("core.longpaths", "true"),
    ("pull.rebase", "true"),
)


class GitRepo:
    """One local checkout of the brain repo; all mutations commit as the configured author."""

    def __init__(
        self,
        path: Path,
        remote: str,
        branch: str,
        ssh_key: Path | None,
        author: tuple[str, str],
        timeout: float,
    ) -> None:
        self.path = path
        self.remote = remote
        self.branch = branch
        self.ssh_key = ssh_key
        self.author = author  # (name, email)
        self.timeout = timeout
        # Test seam: invoked between a post-push-failure pull_rebase and the push retry.
        self._after_pull: Callable[[], None] | None = None

    def _env(self) -> dict[str, str]:
        """Subprocess environment: never prompt, commit as the bot, use the deploy key if present."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        name, email = self.author
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = name
        env["GIT_COMMITTER_EMAIL"] = email
        # Deploy key present -> use exactly it, and ONLY it (IdentitiesOnly).
        if self.ssh_key is not None and self.ssh_key.is_file():
            env["GIT_SSH_COMMAND"] = (
                f'ssh -i "{self.ssh_key}" -o IdentitiesOnly=yes '
                "-o StrictHostKeyChecking=accept-new"
            )
        else:
            # No deploy key configured (tests, throwaway copies): FORCE an SSH command
            # that carries no identity and never prompts, overriding any core.sshCommand
            # baked into a copied checkout's .git/config. Without this, a `shutil.copytree`
            # of the real brain inherits the real key and can silently push to production
            # (a real incident, 2026-07-08). file:// and https remotes ignore SSH entirely,
            # so this is inert for the test suite; a real SSH remote simply fails to auth
            # (push returns pushed=False), which is the correct, safe behavior.
            env["GIT_SSH_COMMAND"] = (
                "ssh -o IdentitiesOnly=yes -o IdentityAgent=none "
                "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o PasswordAuthentication=no"
            )
        return env

    def _exec(self, argv: list[str], verb: str) -> subprocess.CompletedProcess[str]:
        """Run a git argv; raise GitError on timeout, return the CompletedProcess otherwise."""
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {verb} failed: timed out after {self.timeout:g}s") from exc

    def _run_argv(self, argv: list[str], verb: str) -> str:
        proc = self._exec(argv, verb)
        if proc.returncode != 0:
            raise GitError(f"git {verb} failed: {(proc.stderr or '').strip()[:500]}")
        return proc.stdout

    def run(self, *args: str) -> str:
        """Run ``git -C <checkout> <args>``; return stdout, raise GitError on failure."""
        verb = args[0] if args else "git"
        return self._run_argv(["git", "-C", str(self.path), *args], verb)

    def ensure_clone(self) -> None:
        """Clone the remote if the checkout is missing, then enforce local config."""
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # No -C: the checkout directory does not exist yet.
            self._run_argv(
                [
                    "git",
                    "clone",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.longpaths=true",
                    self.remote,
                    str(self.path),
                ],
                "clone",
            )
        for key, value in _LOCAL_CONFIG:
            self.run("config", key, value)

    def head_sha(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def is_dirty(self) -> list[str]:
        """Non-empty ``status --porcelain`` lines (empty list == clean checkout)."""
        return [line for line in self.run("status", "--porcelain").splitlines() if line.strip()]

    def rebase_in_progress(self) -> bool:
        git_dir = self.path / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def abort_rebase(self) -> None:
        """Best-effort ``rebase --abort``; never raises."""
        try:
            self.run("rebase", "--abort")
        except GitError:
            pass

    def pull_rebase(self) -> None:
        """Rebase local commits onto the remote branch; on conflict, abort and teach the caller."""
        try:
            self.run("pull", "--rebase", "origin", self.branch)
        except GitError as exc:
            if self.rebase_in_progress():
                self.abort_rebase()
            raise GitError(_CONFLICT_MSG) from exc

    def commit_and_push(self, paths: list[str], message: str) -> tuple[str, bool]:
        """Stage ``paths``, commit, push. Returns (head_sha, pushed).

        Push rejection triggers one pull --rebase + push retry; a conflict there
        aborts the rebase and raises (the local commit is preserved). A retry
        failure for network-ish reasons returns pushed=False — the commit stays
        local and backlogs onto the next successful write's push.
        """
        self.run("add", "-A", "--", *paths)
        if not self._has_staged_changes():
            return self.head_sha(), True
        self.run("commit", "-m", message)
        try:
            self.run("push", "origin", self.branch)
            return self.head_sha(), True
        except GitError:
            pass
        try:
            self.pull_rebase()
        except GitError as exc:
            raise GitError(
                f"{exc} Your commit is preserved locally and will be pushed "
                "once the conflict is resolved."
            ) from exc
        if self._after_pull is not None:
            self._after_pull()
        sha = self.head_sha()  # the rebase rewrote the commit
        try:
            self.run("push", "origin", self.branch)
            return sha, True
        except GitError:
            return sha, False

    def _has_staged_changes(self) -> bool:
        proc = self._exec(
            ["git", "-C", str(self.path), "diff", "--cached", "--quiet"], "diff"
        )
        return proc.returncode != 0
