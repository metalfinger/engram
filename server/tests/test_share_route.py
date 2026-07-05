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
