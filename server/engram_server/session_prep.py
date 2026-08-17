"""Git plumbing for session preparation — worktrees in the USER'S repos.

Deliberately NOT `gitops.GitRepo`. That class exists for the brain checkout and
forces a `GIT_SSH_COMMAND` carrying the deploy key (or, in tests, no identity at
all). Pointing it at one of Hiren's own repos would override the credentials git
would otherwise use and break perfectly good auth — so this module runs plain
`git -C <repo>` with the environment untouched.

Everything here is local: worktree add, branch listing, log. Nothing pushes and
nothing needs credentials. Synchronous `subprocess.run` for the same reason
gitops gives — asyncio subprocesses break under the Windows selector loop — so
async callers wrap these in `anyio.to_thread.run_sync`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from engram_server.errors import KBError

# A hung git blocks a tool call, and a tool call blocks a conversation. Every
# operation here is local, so anything past a few seconds means something is
# wrong rather than slow.
_TIMEOUT = 30.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, limit: int = 48) -> str:
    """A branch/worktree-safe slug. Empty input is a caller error, not a default —
    a worktree named after nothing is impossible to identify later."""
    s = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    if not s:
        raise KBError(
            "A chunk needs a name with at least one letter or digit — it becomes the "
            "branch, the worktree directory and the thread id."
        )
    return s[:limit].rstrip("-")


def _git(repo: Path, *args: str) -> str:
    """Run `git -C <repo> ...` and return stdout. Raises KBError with git's own
    message, which is almost always more useful than anything we'd invent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise KBError("git is not on PATH — session preparation needs it.") from exc
    except subprocess.TimeoutExpired as exc:
        raise KBError(f"git {args[0] if args else ''} timed out after {_TIMEOUT:g}s.") from exc
    if proc.returncode != 0:
        raise KBError(
            f"git {args[0] if args else ''} failed: {(proc.stderr or '').strip()[:400]}"
        )
    return proc.stdout


def require_repo(repo_path: str) -> Path:
    """Resolve and validate a repo path.

    The server has its OWN working directory and cannot see the caller's, so the
    repo must always be passed explicitly — there is no sensible default to fall
    back on, and guessing would create worktrees in the wrong place."""
    raw = (repo_path or "").strip()
    if not raw:
        raise KBError(
            "repo_path is required — the server cannot see your working directory. "
            "Pass the absolute path of the repo the worktree should belong to."
        )
    repo = Path(raw).expanduser()
    if not repo.is_dir():
        raise KBError(f"No directory at {repo}.")
    try:
        _git(repo, "rev-parse", "--git-dir")
    except KBError as exc:
        raise KBError(f"{repo} is not a git repository ({exc}).") from exc
    return repo.resolve()


def worktree_paths(repo: Path) -> dict[str, str]:
    """Existing worktrees as {path: branch}. Used to make preparation idempotent."""
    out: dict[str, str] = {}
    current = ""
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("branch ") and current:
            out[str(Path(current).resolve())] = line.split("/")[-1].strip()
        elif not line.strip():
            current = ""
    return out


def branch_exists(repo: Path, branch: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        return True
    except KBError:
        return False


def add_worktree(repo: Path, dest: Path, branch: str, base: str) -> None:
    """Create the worktree and its branch off `base`.

    This is the one HARD failure in preparation: everything downstream assumes
    the worktree exists, and a half-prepared chunk is worse than none — it looks
    legitimate."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(repo, branch):
        # Reuse the branch rather than failing: re-preparing a chunk after a
        # crash should land you back where you were, not block you.
        _git(repo, "worktree", "add", str(dest), branch)
        return
    _git(repo, "worktree", "add", str(dest), "-b", branch, base)


def remote_slug(repo: Path) -> str:
    """`owner/name` from origin, for building proof links. Empty when there is no
    remote — a local-only repo is fine, it just has no links to offer."""
    try:
        url = _git(repo, "remote", "get-url", "origin").strip()
    except KBError:
        return ""
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""


def commits_on(repo: Path, branch: str, base: str = "main") -> list[dict[str, str]]:
    """Commits on `branch` that are not on `base`, oldest first.

    Returns [] when the branch has no commits of its own — which is NOT a
    failure. A research chunk legitimately produces no code, and reporting that
    as "nothing shipped" would be false: its output is in the brain."""
    try:
        raw = _git(repo, "log", "--format=%H%x1f%s", f"{base}..{branch}")
    except KBError:
        return []
    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        out.append({"sha": sha.strip()[:12], "subject": subject.strip()})
    out.reverse()
    return out


def is_dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain").strip())
