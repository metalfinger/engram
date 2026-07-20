"""Per-user brain provisioning (v2 M0.2).

Each user gets a self-contained git pair under users_root/<handle>/:

    brain.git   — local bare repo, the user's origin (off-site mirror source, M0.8)
    brain       — working checkout the user's KBStore operates on

The layout mirrors production single-user exactly (checkout + remote), so a
per-user KBStore needs zero special cases — its GitRepo just points at the
local bare instead of GitHub. The operator (Hiren) is NOT provisioned here:
their brain stays at settings.brain_path with the GitHub remote, mapped in by
the store registry (M0.3).

Idempotent by construction: an already-seeded bare is never re-seeded; a
missing checkout is re-cloned from the bare (the bare is the source of truth).

Known M0.3 follow-up: kbstore hardcodes the 'metalfinger' pseudo-project; a
fresh user brain simply shows it as an empty project until the owner-tree name
is parameterized per store.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .errors import KBError
from .gitops import GitRepo
from .tenancy import _HANDLE_RE

_SKILL_REL = Path("skills") / "engram" / "SKILL.md"


class ProvisioningError(KBError):
    """Brain provisioning failed; the checkout/bare pair may be retried safely."""


@dataclass(frozen=True)
class UserBrain:
    handle: str
    bare: Path
    checkout: Path
    remote: str  # file:// URI of the bare — what the user's KBStore uses


def users_root_path(settings: Settings) -> Path:
    return (
        Path(settings.users_root)
        if settings.users_root
        else Path.home() / ".engram" / "users"
    )


def _run_git(argv: list[str], timeout: float) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", *argv],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProvisioningError(f"git {argv[0]} timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        raise ProvisioningError(
            f"git {argv[0]} failed: {(proc.stderr or '').strip()[:500]}"
        )


def _bare_has_commits(bare: Path, timeout: float) -> bool:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    return proc.returncode == 0


def _skeleton(handle: str) -> dict[str, str]:
    """Minimal OKF bundle for a fresh brain. Index files carry no frontmatter;
    everything else grows through kb_write's auto-indexing."""
    return {
        "index.md": (
            f"# {handle}'s brain\n\n"
            "* [Welcome](welcome.md) - what Engram is and what you can do\n"
            "* [Projects](projects/index.md) - active work\n"
            "* [Inbox](inbox/index.md) - quick captures awaiting filing\n"
        ),
        "welcome.md": (
            "---\ntype: reference\ntitle: Welcome to Engram\n"
            f"description: What Engram is and what {handle} can do with it.\n---\n\n"
            "# Welcome to Engram\n\n"
            "This is **your brain** — a private, git-backed knowledge base your AI reads and\n"
            "writes across every session and device. Nothing here is shared unless you say so.\n\n"
            "## What you can do (just ask your Claude)\n\n"
            "- **Remember things that outlive a chat.** Decisions, specs, notes, people — say\n"
            "  \"save this\" and it's written as a concept you'll recall in any future session\n"
            "  (`kb_write`); load a project's state anytime with `kb_load`.\n"
            "- **Pick up where you left off.** Start a fresh session on any device and your\n"
            "  Claude already knows your projects — context, open loops, decisions.\n"
            "- **Connect with people.** `kb_add_contact(\"@handle\")` to connect, then\n"
            "  `kb_dm(\"@handle\", \"…\")` to message them — their Claude delivers it.\n"
            "- **Share knowledge access.** `kb_share_context(\"@handle\", [\"projects/x\"])` lets a\n"
            "  contact's AI read that part of your brain directly — no copy-paste.\n"
            "- **Get pinged.** Install the Engram Chrome extension (Sign in with Engram) for a\n"
            "  desktop alert when someone messages you.\n\n"
            "Delete this note whenever you like — your brain is yours to shape.\n"
        ),
        "projects/index.md": "# Projects\n\nNothing here yet.\n",
        "inbox/index.md": "# Inbox\n\nNothing here yet.\n",
        ".gitattributes": "* text=auto eol=lf\n",
    }


def ensure_user_brain(settings: Settings, handle: str) -> UserBrain:
    """Create (or repair) the bare+checkout pair for ``handle``. Idempotent."""
    # Path-safety gate: the handle becomes a directory name. Tenancy already
    # validated it at account creation; re-check the shape so this function is
    # safe standalone (no '..', separators, or drive letters can pass).
    if not _HANDLE_RE.match(handle):
        raise ProvisioningError(f"Handle {handle!r} is not a valid brain directory name.")

    user_dir = users_root_path(settings) / handle
    bare = user_dir / "brain.git"
    checkout = user_dir / "brain"
    remote = bare.as_uri()

    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["init", "--bare", "--initial-branch", settings.brain_branch, str(bare)],
            settings.git_timeout,
        )

    repo = GitRepo(
        path=checkout,
        remote=remote,
        branch=settings.brain_branch,
        ssh_key=None,  # local file:// remote — no SSH identity wanted
        author=(settings.git_author_name, settings.git_author_email),
        timeout=settings.git_timeout,
    )

    if not _bare_has_commits(bare, settings.git_timeout):
        # First provisioning: clone the empty bare, seed the skeleton, push.
        repo.ensure_clone()
        repo.run("checkout", "-B", settings.brain_branch)
        files = _skeleton(handle)
        for rel, content in files.items():
            target = checkout / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        # Give every brain the canonical protocol skill so kb_load's fingerprint
        # (and skill self-update) works for this user from day one.
        operator_skill = settings.brain_path / _SKILL_REL
        if operator_skill.is_file():
            target = checkout / _SKILL_REL
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(operator_skill.read_bytes())
        repo.commit_and_push(["."], f"chore: provision brain for {handle}")
    else:
        # Bare already seeded — just make sure the checkout exists and is wired.
        repo.ensure_clone()

    return UserBrain(handle=handle, bare=bare, checkout=checkout, remote=remote)


def user_settings(settings: Settings, brain: UserBrain) -> Settings:
    """Settings for a per-user KBStore: same knobs, that user's paths.

    No deploy key: the remote is the local bare, and gitops forces an
    identity-less SSH command when ssh_key is absent, so a per-user store can
    never accidentally push anywhere real.
    """
    return settings.model_copy(
        update={
            "brain_path": brain.checkout,
            "brain_remote": brain.remote,
            "deploy_key_path": Path(str(brain.bare) + ".nokey"),  # never a file
            "tenant_id": brain.handle,  # scopes this store's Qdrant points/queries
        }
    )
