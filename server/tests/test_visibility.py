"""M5.1 — the visibility model: private by default, project default + concept override,
never-public subtrees, secret-scan on publish."""

import pytest

from engram_server.errors import FrontmatterError, KBError
from engram_server.frontmatter import VISIBILITY_VALUES, validate_concept
from engram_server.kbstore import KBStore, _never_public


@pytest.fixture()
async def store(settings):
    s = KBStore(settings)
    await s.start()
    return s


def _concept(desc: str, vis: str | None = None, body: str = "Some content.") -> str:
    v = f"visibility: {vis}\n" if vis else ""
    return f"---\ntype: decision\ndescription: {desc}\n{v}---\n\n# X\n\n{body}\n"


# -- frontmatter validation -------------------------------------------------


def test_valid_visibility_values_pass():
    for v in VISIBILITY_VALUES:
        text, meta, _w = validate_concept(_concept("d", v), rel_path="projects/a/x.md")
        assert meta["visibility"] == v


def test_visibility_is_normalized_to_lowercase():
    _t, meta, _w = validate_concept(_concept("d", "PUBLIC"), rel_path="projects/a/x.md")
    assert meta["visibility"] == "public"


def test_invalid_visibility_is_refused():
    # A typo must fail loudly — silently defaulting to private would be a footgun in
    # the other direction (user believes it's published when it isn't), and 'publik'
    # meaning public would be worse.
    with pytest.raises(FrontmatterError, match="visibility"):
        validate_concept(_concept("d", "publik"), rel_path="projects/a/x.md")


def test_absent_visibility_is_allowed_and_absent():
    _t, meta, _w = validate_concept(_concept("d"), rel_path="projects/a/x.md")
    assert "visibility" not in meta


# -- never-public subtrees --------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "projects/alt/messages/2026-07-x.md",
        "threads/standup.md",
        "workspace/presence/abc.md",
        "inbox/note.md",
    ],
)
def test_never_public_paths(rel):
    assert _never_public(rel) is True


@pytest.mark.parametrize("rel", ["projects/alt/context.md", "projects/alt/decisions/d.md", "self/x.md"])
def test_normal_paths_are_publishable(rel):
    assert _never_public(rel) is False


# -- resolution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_is_private(store):
    await store.kb_write("projects/vis/context.md", _concept("project"), "seed")
    await store.kb_write("projects/vis/d.md", _concept("a decision"), "seed")
    assert await store.effective_visibility("projects/vis/d.md") == "private"


@pytest.mark.asyncio
async def test_project_default_is_inherited(store):
    await store.kb_write("projects/vis2/context.md", _concept("project", "public"), "seed")
    await store.kb_write("projects/vis2/d.md", _concept("a decision"), "seed")
    assert await store.effective_visibility("projects/vis2/d.md") == "public"


@pytest.mark.asyncio
async def test_concept_overrides_project_default(store):
    await store.kb_write("projects/vis3/context.md", _concept("project", "public"), "seed")
    await store.kb_write("projects/vis3/secret.md", _concept("secret", "private"), "seed")
    assert await store.effective_visibility("projects/vis3/secret.md") == "private"


@pytest.mark.asyncio
async def test_messages_never_inherit_a_public_project(store):
    """The load-bearing safety rule: a public project must not publish session mail."""
    await store.kb_write("projects/vis4/context.md", _concept("project", "public"), "seed")
    await store.kb_leave_message("vis4", "A note", "body text", to="any")
    listing = await store.kb_public()
    assert all("/messages/" not in item["path"] for item in listing["public"])


# -- publish ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_and_unpublish(store):
    await store.kb_write("projects/vis5/context.md", _concept("project"), "seed")
    await store.kb_write("projects/vis5/d.md", _concept("a decision"), "seed")

    res = await store.kb_publish("projects/vis5/d.md", "public")
    assert res["visibility"] == "public"
    assert await store.effective_visibility("projects/vis5/d.md") == "public"

    await store.kb_publish("projects/vis5/d.md", "private")
    assert await store.effective_visibility("projects/vis5/d.md") == "private"


@pytest.mark.asyncio
async def test_publishing_a_project_context_sets_the_default(store):
    await store.kb_write("projects/vis6/context.md", _concept("project"), "seed")
    await store.kb_write("projects/vis6/d.md", _concept("a decision"), "seed")
    res = await store.kb_publish("projects/vis6/context.md", "public")
    assert "project" in res["applies_to"]
    assert await store.effective_visibility("projects/vis6/d.md") == "public"


@pytest.mark.asyncio
async def test_publish_refuses_secrets(store):
    await store.kb_write(
        "projects/vis7/creds.md",
        _concept("has a key", body="key is AKIAIOSFODNN7EXAMPLE here"),
        "seed",
    )
    with pytest.raises(KBError, match="secret"):
        await store.kb_publish("projects/vis7/creds.md", "public")
    assert await store.effective_visibility("projects/vis7/creds.md") == "private"


@pytest.mark.asyncio
async def test_publish_refuses_never_public_paths(store):
    await store.kb_write("projects/vis8/context.md", _concept("project"), "seed")
    await store.kb_leave_message("vis8", "A note", "body", to="any")
    msgs = [p for p in (await store.kb_load("vis8"))["unread_messages"]]
    assert msgs, "expected a seeded message"
    with pytest.raises(KBError, match="never be published"):
        await store.kb_publish(msgs[0]["path"], "public")


@pytest.mark.asyncio
async def test_invalid_visibility_arg_refused(store):
    await store.kb_write("projects/vis9/d.md", _concept("a decision"), "seed")
    with pytest.raises(KBError, match="visibility must be"):
        await store.kb_publish("projects/vis9/d.md", "everyone")


@pytest.mark.asyncio
async def test_kb_public_audit_lists_both_tiers(store):
    await store.kb_write("projects/vis-a/pub.md", _concept("public one", "public"), "seed")
    await store.kb_write("projects/vis-a/contacts-tier.md", _concept("contacts one", "contacts"), "seed")
    await store.kb_write("projects/vis-a/priv.md", _concept("private one"), "seed")
    listing = await store.kb_public()
    pub = {i["path"] for i in listing["public"]}
    con = {i["path"] for i in listing["contacts"]}
    assert "projects/vis-a/pub.md" in pub
    assert "projects/vis-a/contacts-tier.md" in con
    assert not any("priv.md" in p for p in pub | con)
