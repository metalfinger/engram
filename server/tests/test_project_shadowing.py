"""A hollow directory must not shadow a real project.

Found in the field: an empty `projects/engram/` left over from a July engine
retest shadowed `projects/personal/engram/` for six weeks. kb_projects listed the
hollow one, kb_load returned context_md: null, and every session orienting on the
project got nothing — while search kept finding the real content, because it
walks files rather than the registry. A directory that only LOOKS like a project
is worse than a missing one: nothing reports an error.
"""

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _project(root, rel: str, title: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "context.md").write_text(
        f"---\ntype: project\ntitle: {title}\ndescription: {title} desc\n"
        f"status: active\n---\n\n# About\n\nReal.\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_a_real_project_in_a_folder_beats_a_hollow_top_level_dir(store, settings):
    root = settings.brain_path
    _project(root, "projects/personal/thing", "Thing")
    hollow = root / "projects" / "thing"
    hollow.mkdir(parents=True, exist_ok=True)
    (hollow / "log.md").write_text("# Log — thing\n", encoding="utf-8")

    loaded = await store.kb_load("thing", lite=True)
    assert loaded["context_md"], "the real project must win over a hollow directory"
    assert "Real." in loaded["context_md"]


@pytest.mark.asyncio
async def test_a_genuine_top_level_project_still_resolves(store, settings):
    _project(settings.brain_path, "projects/plain", "Plain")
    loaded = await store.kb_load("plain", lite=True)
    assert loaded["context_md"] and "Real." in loaded["context_md"]


@pytest.mark.asyncio
async def test_a_project_with_no_context_yet_still_resolves(store, settings):
    """A project mid-creation legitimately has no context.md — resolution must not
    drop it, or kb_attach_project could never bootstrap one."""
    d = settings.brain_path / "projects" / "brandnew"
    d.mkdir(parents=True, exist_ok=True)
    (d / "log.md").write_text("# Log — brandnew\n", encoding="utf-8")
    loaded = await store.kb_load("brandnew", lite=True)
    assert loaded["project"] == "brandnew"
