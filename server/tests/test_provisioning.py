"""M0.2 — per-user brain provisioning (bare + checkout pair under users_root)."""

import os
import shutil
import stat
import subprocess

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.provisioning import (
    ProvisioningError,
    ensure_user_brain,
    user_settings,
    users_root_path,
)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        _env_file=None,
        brain_path=tmp_path / "operator-brain",
        brain_remote="file:///nonexistent",  # operator remote is never touched here
        brain_branch="main",
        deploy_key_path=tmp_path / "no-such-key",
        users_root=str(tmp_path / "users"),
        pull_ttl=0.0,
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


def test_provision_creates_pair_and_skeleton(settings):
    brain = ensure_user_brain(settings, "amiyanshu")

    assert brain.bare.is_dir() and (brain.checkout / ".git").is_dir()
    assert brain.bare == users_root_path(settings) / "amiyanshu" / "brain.git"

    index = (brain.checkout / "index.md").read_text(encoding="utf-8")
    assert "amiyanshu's brain" in index
    welcome = (brain.checkout / "welcome.md").read_text(encoding="utf-8")
    assert "Welcome to Engram" in welcome
    # v3: the welcome is a RUNNABLE first session, not a feature tour
    assert "kb_attach_project" in welcome and "kb_room_open" in welcome
    assert (brain.checkout / "projects" / "index.md").is_file()
    assert (brain.checkout / "inbox" / "index.md").is_file()
    assert (brain.checkout / ".gitattributes").is_file()

    assert _git(brain.checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(brain.checkout, "log", "-1", "--format=%an") == settings.git_author_name
    # seed commit reached the bare (the user's origin)
    assert _git(brain.bare, "rev-parse", "HEAD") == _git(brain.checkout, "rev-parse", "HEAD")


def test_provision_is_idempotent(settings):
    first = ensure_user_brain(settings, "amiyanshu")
    head = _git(first.checkout, "rev-parse", "HEAD")
    second = ensure_user_brain(settings, "amiyanshu")
    assert second.checkout == first.checkout
    assert _git(second.checkout, "rev-parse", "HEAD") == head


def test_missing_checkout_is_recloned_from_bare(settings):
    brain = ensure_user_brain(settings, "amiyanshu")
    head = _git(brain.bare, "rev-parse", "HEAD")
    # Windows: git object files are read-only — chmod them writable on error.
    shutil.rmtree(
        brain.checkout,
        onexc=lambda fn, path, exc: (os.chmod(path, stat.S_IWRITE), fn(path)),
    )

    again = ensure_user_brain(settings, "amiyanshu")
    assert (again.checkout / "index.md").is_file()
    assert _git(again.checkout, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "A B", "", "-lead"])
def test_unsafe_handles_rejected(settings, bad):
    with pytest.raises(ProvisioningError):
        ensure_user_brain(settings, bad)


def test_operator_skill_is_copied_when_present(settings):
    skill = settings.brain_path / "skills" / "engram" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: engram\n---\nprotocol\n", encoding="utf-8")

    brain = ensure_user_brain(settings, "withskill")
    copied = brain.checkout / "skills" / "engram" / "SKILL.md"
    assert copied.is_file()
    assert copied.read_bytes() == skill.read_bytes()


@pytest.mark.asyncio
async def test_kbstore_runs_on_a_provisioned_brain(settings):
    brain = ensure_user_brain(settings, "amiyanshu")
    store = KBStore(user_settings(settings, brain))
    await store.start()

    result = await store.kb_write(
        "projects/demo/context.md",
        "---\ntype: project\ndescription: demo project\n---\n\n# About\n\nDemo.\n",
        "feat: demo project",
    )
    assert result["pushed"] is True

    projects = await store.kb_projects()
    assert "demo" in [p["id"] for p in projects]
    # the write landed in the user's own bare, not anywhere shared
    assert _git(brain.bare, "rev-parse", "HEAD") == _git(brain.checkout, "rev-parse", "HEAD")
