"""Off-site backup mirror (v2 M0.8).

Nightly job: mirror every user's bare brain repo to ONE private off-site git
remote, each user parked on branch ``users/<handle>`` — one repo, one deploy
key, no per-user repo creation on the remote side. The operator's own brain is
excluded on purpose: it lives outside ``users_root`` and already pushes to its
own GitHub remote on every write, so it is never a candidate here.

Force-push is correct: the mirror is a copy, never a source of truth, so each
user's branch is simply overwritten to match their bare's current HEAD. One
user's push failure (corrupt bare, transient network error, ...) must never
stop the rest — failures are collected per-handle and returned, never raised.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import Settings
from .provisioning import users_root_path

log = logging.getLogger("engram.backup")


def _env(settings: Settings) -> dict[str, str]:
    """Subprocess environment for the mirror push — same shape as
    ``gitops.GitRepo._env``: never prompt, use the deploy key (and only it)
    when one is configured, otherwise force an identity-less SSH command so a
    stray agent/config identity can never leak into an off-site push."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    key = Path(settings.deploy_key_path)
    if key.is_file():
        env["GIT_SSH_COMMAND"] = (
            f'ssh -i "{key}" -o IdentitiesOnly=yes '
            "-o StrictHostKeyChecking=accept-new"
        )
    else:
        env["GIT_SSH_COMMAND"] = (
            "ssh -o IdentitiesOnly=yes -o IdentityAgent=none "
            "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o PasswordAuthentication=no"
        )
    return env


def _push_one(
    bare: Path, remote: str, branch: str, handle: str, timeout: float, env: dict[str, str]
) -> None:
    """Force-push ``bare``'s ``branch`` to ``remote`` as ``refs/heads/users/<handle>``.

    Raises ``RuntimeError`` with a truncated stderr on failure (bad ref, timeout,
    auth, network — the caller treats them all the same: this user failed, move on).
    """
    refspec = f"{branch}:refs/heads/users/{handle}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(bare), "push", "--force", remote, refspec],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"push timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[:200])


def mirror_all(settings: Settings) -> dict:
    """Mirror every user bare under ``users_root`` to ``settings.backup_remote``.

    Synchronous (shells out to git) — callers wrap this in a thread. Returns
    ``{"enabled": bool, "mirrored": [handle, ...], "failed": [{"handle", "error"}, ...],
    "remote": backup_remote}``. Disabled (empty ``backup_remote``) is a no-op that
    touches nothing and omits ``remote``.
    """
    remote = (settings.backup_remote or "").strip()
    if not remote:
        return {"enabled": False, "mirrored": [], "failed": []}

    root = users_root_path(settings)
    mirrored: list[str] = []
    failed: list[dict[str, str]] = []

    if root.is_dir():
        env = _env(settings)
        for user_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            bare = user_dir / "brain.git"
            if not bare.is_dir():
                continue
            handle = user_dir.name
            try:
                _push_one(bare, remote, settings.brain_branch, handle, settings.git_timeout, env)
                mirrored.append(handle)
            except RuntimeError as exc:
                failed.append({"handle": handle, "error": str(exc)})

    log.info(
        "backup mirror: %d ok, %d failed -> %s", len(mirrored), len(failed), remote
    )
    return {"enabled": True, "mirrored": mirrored, "failed": failed, "remote": remote}
