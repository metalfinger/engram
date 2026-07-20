"""M0.8 — off-site backup mirror (nightly mirror of every user bare -> one remote)."""

import subprocess

import pytest

from engram_server.backup import mirror_all
from engram_server.config import Settings
from engram_server.provisioning import ensure_user_brain


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


@pytest.fixture()
def mirror_bare(tmp_path):
    """The one off-site remote every user's branch lands on."""
    dest = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(dest)],
        capture_output=True, encoding="utf-8", check=True,
    )
    return dest


@pytest.fixture()
def settings(tmp_path, mirror_bare):
    return Settings(
        _env_file=None,
        brain_path=tmp_path / "operator-brain",  # must NOT be touched by mirror_all
        brain_remote="file:///nonexistent",
        brain_branch="main",
        deploy_key_path=tmp_path / "no-such-key",
        users_root=str(tmp_path / "users"),
        pull_ttl=0.0,
        backup_remote=str(mirror_bare),
    )


def test_disabled_when_remote_empty(tmp_path):
    settings = Settings(
        _env_file=None,
        brain_path=tmp_path / "operator-brain",
        users_root=str(tmp_path / "users"),
        backup_remote="",
    )
    result = mirror_all(settings)
    assert result == {"enabled": False, "mirrored": [], "failed": []}
    # nothing was even discovered/created
    assert not (tmp_path / "users").exists()


def test_mirrors_both_users(settings):
    alice = ensure_user_brain(settings, "alice")
    bob = ensure_user_brain(settings, "bob")

    result = mirror_all(settings)

    assert result["enabled"] is True
    assert set(result["mirrored"]) == {"alice", "bob"}
    assert result["failed"] == []
    assert result["remote"] == settings.backup_remote

    assert _git(settings.backup_remote, "rev-parse", "refs/heads/users/alice") == _git(
        alice.bare, "rev-parse", "HEAD"
    )
    assert _git(settings.backup_remote, "rev-parse", "refs/heads/users/bob") == _git(
        bob.bare, "rev-parse", "HEAD"
    )
    # the operator brain is excluded — never touched
    assert not settings.brain_path.exists()


def test_rerun_is_idempotent(settings):
    alice = ensure_user_brain(settings, "alice")

    first = mirror_all(settings)
    second = mirror_all(settings)

    assert first["mirrored"] == second["mirrored"] == ["alice"]
    assert second["failed"] == []
    assert _git(settings.backup_remote, "rev-parse", "refs/heads/users/alice") == _git(
        alice.bare, "rev-parse", "HEAD"
    )


def test_one_user_failure_does_not_block_others(settings):
    alice = ensure_user_brain(settings, "alice")
    bob = ensure_user_brain(settings, "bob")

    # Corrupt alice's bare so her push has nothing to resolve: drop the loose
    # ref for her branch (the source side of the push refspec).
    branch_ref = alice.bare / "refs" / "heads" / settings.brain_branch
    assert branch_ref.is_file()
    branch_ref.unlink()

    result = mirror_all(settings)

    assert result["mirrored"] == ["bob"]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["handle"] == "alice"
    assert result["failed"][0]["error"]  # non-empty, truncated git stderr

    assert _git(settings.backup_remote, "rev-parse", "refs/heads/users/bob") == _git(
        bob.bare, "rev-parse", "HEAD"
    )
    # no stale/partial ref for alice was left behind
    proc = subprocess.run(
        ["git", "-C", str(settings.backup_remote), "rev-parse", "--verify", "refs/heads/users/alice"],
        capture_output=True, encoding="utf-8",
    )
    assert proc.returncode != 0


def test_new_commit_advances_the_mirror(settings):
    alice = ensure_user_brain(settings, "alice")
    mirror_all(settings)
    old_sha = _git(settings.backup_remote, "rev-parse", "refs/heads/users/alice")

    # Simulate a normal kb_write: commit in the checkout, push to the bare.
    (alice.checkout / "note.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(alice.checkout), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(alice.checkout),
            "-c", "user.name=test", "-c", "user.email=test@test.local",
            "commit", "-m", "note",
        ],
        capture_output=True, encoding="utf-8", check=True,
    )
    subprocess.run(
        ["git", "-C", str(alice.checkout), "push", "origin", settings.brain_branch],
        capture_output=True, encoding="utf-8", check=True,
    )
    new_sha = _git(alice.bare, "rev-parse", "HEAD")
    assert new_sha != old_sha

    result = mirror_all(settings)

    assert result["mirrored"] == ["alice"]
    assert _git(settings.backup_remote, "rev-parse", "refs/heads/users/alice") == new_sha
