"""Real project folders: projects/<folder>/<id> directories, flat IDs, links kept correct.

The link cases are the point. A move changes a project's DEPTH, so any link that escapes
the project (../../library/...) must be re-expressed — string replacement can't do that.
"""

import pytest

from engram_server.errors import KBError
from engram_server.kbstore import KBStore


@pytest.fixture()
async def store(settings):
    s = KBStore(settings)
    await s.start()
    return s


def _doc(desc: str, body: str = "Body.") -> str:
    return f"---\ntype: project\ndescription: {desc}\n---\n\n# About\n\n{body}\n"


# -- resolution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_into_folder_and_back(store):
    await store.kb_write("projects/alpha/context.md", _doc("alpha"), "seed")
    res = await store.kb_move_project("alpha", "personal")
    assert res["to"] == "projects/personal/alpha"
    assert (store.root / "projects" / "personal" / "alpha" / "context.md").is_file()
    assert not (store.root / "projects" / "alpha").exists()

    # the ID still resolves — nothing that refers to the project by id breaks
    assert store._project_rel("alpha") == "projects/personal/alpha"
    loaded = await store.kb_load("alpha")
    assert loaded["project"] == "alpha"

    back = await store.kb_move_project("alpha", "")
    assert back["to"] == "projects/alpha"
    assert store._project_rel("alpha") == "projects/alpha"


@pytest.mark.asyncio
async def test_projects_report_their_folder(store):
    await store.kb_write("projects/beta/context.md", _doc("beta"), "seed")
    await store.kb_write("projects/gamma/context.md", _doc("gamma"), "seed")
    await store.kb_move_project("beta", "alt-inc")
    listing = {p["id"]: p for p in await store.kb_projects()}
    assert listing["beta"]["folder"] == "alt-inc"
    assert listing["gamma"]["folder"] == ""
    assert {"beta", "gamma"} <= set(listing)  # both still listed, wherever they live


@pytest.mark.asyncio
async def test_a_folder_is_not_mistaken_for_a_project(store):
    await store.kb_write("projects/delta/context.md", _doc("delta"), "seed")
    await store.kb_move_project("delta", "personal")
    ids = [p["id"] for p in await store.kb_projects()]
    assert "delta" in ids
    assert "personal" not in ids  # the folder itself is not a project


# -- the link cases ---------------------------------------------------------


@pytest.mark.asyncio
async def test_links_inside_the_project_survive_untouched(store):
    await store.kb_write("projects/eps/context.md", _doc("eps"), "seed")
    await store.kb_write(
        "projects/eps/decisions/d1.md",
        "---\ntype: decision\ndescription: one\n---\n\n# D1\n\nSee [ctx](../context.md).\n",
        "seed",
    )
    await store.kb_move_project("eps", "personal")
    text = (store.root / "projects/personal/eps/decisions/d1.md").read_text(encoding="utf-8")
    assert "](../context.md)" in text  # same relative position -> unchanged
    got = await store.kb_read("projects/personal/eps/decisions/d1.md")
    assert got["path"] == "projects/personal/eps/decisions/d1.md"


@pytest.mark.asyncio
async def test_links_escaping_the_project_are_re_expressed(store):
    """The case regex replacement gets wrong: depth changes, so ../../ must become ../../../."""
    await store.kb_write("library/runbooks/how-to.md",
                         "---\ntype: runbook\ndescription: r\n---\n\n# R\n\nSteps.\n", "seed")
    await store.kb_write(
        "projects/zeta/context.md",
        _doc("zeta", "See [the runbook](../../library/runbooks/how-to.md)."),
        "seed",
    )
    await store.kb_move_project("zeta", "alt-inc")

    text = (store.root / "projects/alt-inc/zeta/context.md").read_text(encoding="utf-8")
    assert "](../../../library/runbooks/how-to.md)" in text
    # and it genuinely resolves to the real file
    target = (store.root / "projects/alt-inc/zeta" / "../../../library/runbooks/how-to.md").resolve()
    assert target == (store.root / "library/runbooks/how-to.md").resolve()


@pytest.mark.asyncio
async def test_links_pointing_INTO_the_moved_project_are_fixed(store):
    await store.kb_write("projects/eta/context.md", _doc("eta"), "seed")
    await store.kb_write(
        "projects/theta/context.md",
        _doc("theta", "See [eta](../eta/context.md)."),
        "seed",
    )
    await store.kb_move_project("eta", "personal")

    text = (store.root / "projects/theta/context.md").read_text(encoding="utf-8")
    assert "](../personal/eta/context.md)" in text
    target = (store.root / "projects/theta" / "../personal/eta/context.md").resolve()
    assert target == (store.root / "projects/personal/eta/context.md").resolve()


@pytest.mark.asyncio
async def test_move_leaves_the_checkout_clean(store):
    """Regression (hit in production): links rewritten OUTSIDE the moved tree —
    library/, self/, metalfinger/ — must be staged too. An unstaged rewrite leaves
    the checkout dirty, and a dirty checkout blocks EVERY later write."""
    await store.kb_write("library/runbooks/how.md",
                         "---\ntype: runbook\ndescription: r\n---\n\n# R\n\nSteps.\n", "seed")
    await store.kb_write("projects/omega/context.md", _doc("omega"), "seed")
    # a file OUTSIDE projects/ that links INTO the project being moved
    await store.kb_write(
        "self/stack.md",
        "---\ntype: reference\ndescription: stack\n---\n\n# Stack\n\n"
        "See [omega](../projects/omega/context.md).\n",
        "seed",
    )

    await store.kb_move_project("omega", "personal")

    dirty = store.repo.is_dirty()
    assert dirty == [], f"checkout left dirty after move: {dirty}"
    # and the outside link actually got fixed
    text = (store.root / "self/stack.md").read_text(encoding="utf-8")
    assert "](../projects/personal/omega/context.md)" in text


@pytest.mark.asyncio
async def test_external_links_are_left_alone(store):
    await store.kb_write(
        "projects/iota/context.md",
        _doc("iota", "See [site](https://example.com/x) and [anchor](#section)."),
        "seed",
    )
    await store.kb_move_project("iota", "personal")
    text = (store.root / "projects/personal/iota/context.md").read_text(encoding="utf-8")
    assert "](https://example.com/x)" in text and "](#section)" in text


# -- guards -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_nest_a_project_inside_another_project(store):
    await store.kb_write("projects/kappa/context.md", _doc("kappa"), "seed")
    await store.kb_write("projects/lambda-p/context.md", _doc("lambda"), "seed")
    with pytest.raises(KBError, match="not a folder"):
        await store.kb_move_project("kappa", "lambda-p")


@pytest.mark.asyncio
async def test_bad_folder_names_and_unknown_projects_refused(store):
    await store.kb_write("projects/mu/context.md", _doc("mu"), "seed")
    for bad in ("Has Space", "../evil", "a/b", "dot.dot"):
        with pytest.raises(KBError):
            await store.kb_move_project("mu", bad)
    with pytest.raises(KBError, match="Unknown project"):
        await store.kb_move_project("nope-not-here", "personal")


@pytest.mark.asyncio
async def test_folder_name_case_is_normalized(store):
    """'Personal' and 'personal' are the same folder — like handles, case is normalized
    rather than rejected, so the same folder can't exist twice."""
    await store.kb_write("projects/xi/context.md", _doc("xi"), "seed")
    res = await store.kb_move_project("xi", "Personal")
    assert res["folder"] == "personal"
    assert (store.root / "projects/personal/xi").is_dir()


@pytest.mark.asyncio
async def test_folder_gets_an_index(store):
    await store.kb_write("projects/nu/context.md", _doc("nu the project"), "seed")
    await store.kb_move_project("nu", "personal")
    index = (store.root / "projects/personal/index.md").read_text(encoding="utf-8")
    assert "nu/context.md" in index and "nu the project" in index
