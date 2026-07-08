"""kb_edit surgical body operations, zero-match errors, and frontmatter protection."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.kbstore import KBStore

PATH = "projects/alt/specs/api.md"
SPEC = (
    "---\ntype: spec\ntitle: API\ndescription: The API spec.\n---\n"
    "# API\n\nVersion v1.0.0 here.\n\n## Notes\n\nOld notes.\n\n## Refs\n\n- one\n"
)


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    await s.kb_write(PATH, SPEC, "seed spec")
    return s


def _body(settings: Settings) -> str:
    text = (settings.brain_path / PATH).read_text(encoding="utf-8")
    return text.split("---\n", 2)[2]  # everything after the closing frontmatter fence


async def test_append(store: KBStore, settings: Settings) -> None:
    r = await store.kb_edit(PATH, "append", "Appended line.")
    assert r["operation"] == "append"
    assert _body(settings).rstrip().endswith("Appended line.")
    # frontmatter fence untouched
    assert (settings.brain_path / PATH).read_text(encoding="utf-8").startswith("---\ntype: spec\n")


async def test_prepend(store: KBStore, settings: Settings) -> None:
    await store.kb_edit(PATH, "prepend", "Top line.")
    assert _body(settings).lstrip().startswith("Top line.")


async def test_find_replace_first(store: KBStore, settings: Settings) -> None:
    await store.kb_edit(PATH, "find_replace", "v2.0.0", find="v1.0.0")
    body = _body(settings)
    assert "v2.0.0" in body and "v1.0.0" not in body


async def test_find_replace_all(store: KBStore, settings: Settings) -> None:
    await store.kb_edit(PATH, "append", "one more one and one.")
    await store.kb_edit(PATH, "find_replace", "ONE", find="one", occurrence="all")
    assert "one" not in _body(settings)


async def test_find_replace_zero_match_errors(store: KBStore) -> None:
    with pytest.raises(KBError, match="not found in the body"):
        await store.kb_edit(PATH, "find_replace", "x", find="no-such-anchor")


async def test_find_replace_frontmatter_is_protected(store: KBStore) -> None:
    # 'type: spec' lives only in the frontmatter — kb_edit must refuse, not silently miss.
    with pytest.raises(KBError, match="frontmatter"):
        await store.kb_edit(PATH, "find_replace", "y", find="type: spec")


async def test_replace_section(store: KBStore, settings: Settings) -> None:
    await store.kb_edit(PATH, "replace_section", "Fresh notes.", section="## Notes")
    body = _body(settings)
    assert "Fresh notes." in body
    assert "Old notes." not in body
    assert "## Refs" in body  # the following section is preserved


async def test_replace_section_missing_errors(store: KBStore) -> None:
    with pytest.raises(KBError, match="No section heading"):
        await store.kb_edit(PATH, "replace_section", "x", section="## Nonexistent")


async def test_insert_after_and_before(store: KBStore, settings: Settings) -> None:
    await store.kb_edit(PATH, "insert_after", "- two", find="- one")
    body = _body(settings)
    assert body.index("- one") < body.index("- two")
    await store.kb_edit(PATH, "insert_before", "- zero", find="- one")
    body = _body(settings)
    assert body.index("- zero") < body.index("- one")


async def test_insert_zero_match_errors(store: KBStore) -> None:
    with pytest.raises(KBError, match="not found in the body"):
        await store.kb_edit(PATH, "insert_after", "x", find="no-such-line")


async def test_rejects_reserved_and_missing(store: KBStore) -> None:
    with pytest.raises(KBError, match="index.md"):
        await store.kb_edit("projects/alt/index.md", "append", "x")
    with pytest.raises(KBError, match="kb_append_log"):
        await store.kb_edit("projects/alt/log.md", "append", "x")
    with pytest.raises(KBError, match="kb_leave_message"):
        await store.kb_edit("projects/alt/messages/2026-07-note.md", "append", "x")
    with pytest.raises(KBError, match="No such concept"):
        await store.kb_edit("projects/alt/specs/ghost.md", "append", "x")


async def test_unknown_operation(store: KBStore) -> None:
    with pytest.raises(KBError, match="Unknown operation"):
        await store.kb_edit(PATH, "frobnicate", "x")


async def test_noop_edit_makes_no_commit(store: KBStore) -> None:
    sha_before = store.repo.head_sha()
    await store.kb_edit(PATH, "append", "")  # empty append changes nothing
    assert store.repo.head_sha() == sha_before
