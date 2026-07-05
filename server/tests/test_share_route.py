"""The public /share/{token} route: serves ONLY explicitly-shared artifact bodies,
leaks no paths/nav, rejects short/unknown tokens, and never bypasses the Access guard
on the private routes.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.config import Settings
from engram_server.explorer import register as register_explorer
from engram_server.kbstore import KBStore

SHARED_ARTIFACT = (
    "---\ntype: artifact\ntitle: Weekly Status\ndescription: The weekly status report.\n"
    "sources:\n  - projects/alt/context.md\n  - projects/alt/decisions/index.md\n---\n\n"
    "# Weekly Status\n\nEverything is on track. See [related](../context.md).\n"
)


def _client(settings: Settings) -> TestClient:
    """A Starlette app carrying only the explorer's custom routes (guard included)."""
    mcp = FastMCP("test-share")
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


async def _seed_shared_artifact(settings: Settings) -> str:
    store = KBStore(settings)
    await store.start()
    path = "projects/alt/artifacts/2026-07-weekly.md"
    await store.kb_write(path, SHARED_ARTIFACT, "save weekly")
    res = await store.kb_share_artifact(path)
    return res["share_url"].rsplit("/", 1)[-1]  # the token


async def test_share_route_serves_body_without_leaking_paths(settings: Settings) -> None:
    token = await _seed_shared_artifact(settings)
    resp = _client(settings).get(f"/share/{token}")
    assert resp.status_code == 200
    html = resp.text
    # the shared body IS served
    assert "Weekly Status" in html
    assert "Everything is on track" in html
    assert "Shared from Hiren" in html  # neutral footer
    # NOTHING else: no source paths, no nav/sidebar/search, no /brain/f links
    assert "projects/alt/context.md" not in html
    assert "projects/alt/decisions/index.md" not in html
    assert "/brain/f/" not in html
    assert "Search the brain" not in html
    assert "/brain/system" not in html


async def test_share_route_rejects_short_and_unknown_tokens(settings: Settings) -> None:
    await _seed_shared_artifact(settings)  # ensures a checkout exists to scan
    client = _client(settings)
    assert client.get("/share/short").status_code == 404  # < 20 chars, rejected outright
    assert client.get("/share/" + "z" * 40).status_code == 404  # valid length, no match


async def test_share_route_404_when_no_checkout(settings: Settings) -> None:
    # No brain checkout on disk at all: a long token still 404s, never 500s.
    assert _client(settings).get("/share/" + "a" * 40).status_code == 404


async def test_private_routes_stay_guarded(settings: Settings) -> None:
    # The share route is unguarded, but the Access gate on every private route holds:
    # no Cf-Access JWT → 403 (settings default dev_no_access=False).
    client = _client(settings)
    for path in ("/brain", "/brain/system", "/brain/activity", "/brain/setup"):
        assert client.get(path).status_code == 403, path


async def test_artifacts_gallery_lists_with_badges(settings: Settings) -> None:
    await _seed_shared_artifact(settings)
    open_settings = settings.model_copy(update={"dev_no_access": True})
    resp = _client(open_settings).get("/brain/artifacts")
    assert resp.status_code == 200
    html = resp.text
    assert "Artifacts" in html
    assert "Weekly Status" in html
    assert "alt" in html  # project chip
    assert ("current" in html) or ("sources changed" in html)  # staleness badge
    assert "shared" in html  # share badge (we shared it in seeding)


async def test_artifacts_gallery_stays_guarded(settings: Settings) -> None:
    await _seed_shared_artifact(settings)
    resp = _client(settings).get("/brain/artifacts")
    assert resp.status_code == 403


HTML_ARTIFACT = (
    "---\ntype: artifact\ntitle: Interactive Brief\ndescription: A full HTML experience.\n"
    "format: html\nsources:\n  - projects/alt/context.md\n---\n\n"
    "<!DOCTYPE html>\n<html><head><style>body{background:#000}</style></head>"
    "<body><canvas id=viz></canvas><script>console.log('hi')</script></body></html>\n"
)


async def test_share_route_serves_html_artifact_verbatim_sandboxed(settings: Settings) -> None:
    store = KBStore(settings)
    await store.start()
    path = "projects/alt/artifacts/2026-07-interactive.md"
    await store.kb_write(path, HTML_ARTIFACT, "save html artifact")
    token = (await store.kb_share_artifact(path))["share_url"].rsplit("/", 1)[-1]

    resp = _client(settings).get(f"/share/{token}")
    assert resp.status_code == 200
    # the REAL page, verbatim — not a markdown rendering of it
    assert resp.text.strip().startswith("<!DOCTYPE html>")
    assert "<canvas id=viz>" in resp.text
    assert "Shared from Hiren" not in resp.text  # no wrapper shell
    # sandboxed: unique origin, no cookie/storage access from shared content
    assert "sandbox" in resp.headers.get("content-security-policy", "")


async def test_artifact_update_preserves_share_token(settings: Settings) -> None:
    store = KBStore(settings)
    await store.start()
    path = "projects/alt/artifacts/2026-07-keeper.md"
    await store.kb_write(path, SHARED_ARTIFACT, "save v1")
    token = (await store.kb_share_artifact(path))["share_url"].rsplit("/", 1)[-1]
    # re-save WITHOUT the share field (a chat session won't know about it)
    await store.kb_write(path, SHARED_ARTIFACT.replace("on track", "on track v2"), "save v2")
    resp = _client(settings).get(f"/share/{token}")
    assert resp.status_code == 200
    assert "on track v2" in resp.text
