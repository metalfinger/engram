"""Recipe listing at the KBStore layer: kb_recipes surfaces type: recipe concepts
under projects/*/recipes/ with their sources + instruction, newest first."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore

RECIPE = (
    "---\ntype: recipe\ntitle: Weekly Status Recipe\ndescription: Rebuild the weekly status.\n"
    "sources:\n  - projects/alt/context.md\n"
    'instruction: "Summarize the current state of alt into a one-page weekly status."\n'
    "---\n\n[ctx](../context.md)\n"
)


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_kb_recipes_lists_with_sources_and_instruction(store: KBStore) -> None:
    await store.kb_write("projects/alt/recipes/2026-07-weekly.md", RECIPE, "save recipe")
    recipes = await store.kb_recipes()
    r = next(x for x in recipes if x["path"].endswith("2026-07-weekly.md"))
    assert r["project"] == "alt"
    assert r["title"] == "Weekly Status Recipe"
    assert r["sources"] == ["projects/alt/context.md"]
    assert r["instruction"] == "Summarize the current state of alt into a one-page weekly status."
    assert r["path"] == "projects/alt/recipes/2026-07-weekly.md"


async def test_kb_recipes_project_filter(store: KBStore) -> None:
    await store.kb_write("projects/alt/recipes/2026-07-weekly.md", RECIPE, "save recipe")
    assert [r["project"] for r in await store.kb_recipes(project="alt")] == ["alt"]
    assert await store.kb_recipes(project="hyprlocl") == []


async def test_kb_recipes_empty_when_none(store: KBStore) -> None:
    assert await store.kb_recipes() == []


async def test_kb_recipes_newest_first(store: KBStore) -> None:
    older = RECIPE.replace("type: recipe\n", "type: recipe\ntimestamp: 2026-06-01T00:00:00Z\n")
    newer = RECIPE.replace("type: recipe\n", "type: recipe\ntimestamp: 2026-07-01T00:00:00Z\n")
    await store.kb_write("projects/alt/recipes/2026-06-old.md", older, "save old")
    await store.kb_write("projects/alt/recipes/2026-07-new.md", newer, "save new")
    recipes = await store.kb_recipes(project="alt")
    paths = [r["path"] for r in recipes]
    assert paths.index("projects/alt/recipes/2026-07-new.md") < paths.index(
        "projects/alt/recipes/2026-06-old.md"
    )


async def test_kb_recipes_ignores_index_and_non_recipe_dirs(store: KBStore) -> None:
    await store.kb_write("projects/alt/recipes/2026-07-weekly.md", RECIPE, "save recipe")
    # An artifact under artifacts/ must not show up in the recipe listing.
    await store.kb_write(
        "projects/alt/artifacts/2026-07-a.md",
        "---\ntype: artifact\ndescription: An artifact.\nsources:\n  - projects/alt/context.md\n---\n\nBody.\n",
        "save artifact",
    )
    paths = [r["path"] for r in await store.kb_recipes()]
    assert paths == ["projects/alt/recipes/2026-07-weekly.md"]
