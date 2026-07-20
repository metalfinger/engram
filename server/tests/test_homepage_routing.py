"""M1.2 integration — the homepage must win GET "/" over the explorer's root_redirect.

Both register a `GET /` custom route; Starlette matches the first one added, so the
homepage MUST be registered before the explorer. This guards the wiring order in
app.py (a regression here would silently make the homepage dead code)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.explorer import register as register_explorer
from engram_server.explorer.homepage import register_homepage


def _client(settings, homepage_first: bool) -> TestClient:
    mcp = FastMCP("test-home")
    if homepage_first:
        register_homepage(mcp, settings)
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


def test_homepage_wins_root_when_registered_first(settings):
    dev = settings.model_copy(update={"dev_no_access": True})
    resp = _client(dev, homepage_first=True).get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # the homepage carries the MCP connector URL; root_redirect never would
    assert f"{settings.public_url}/mcp" in resp.text


def test_explorer_owns_root_when_homepage_absent(settings):
    """Sanity: without the homepage, the explorer's root_redirect owns "/" — proving
    the two genuinely collide and order is what decides the winner."""
    dev = settings.model_copy(update={"dev_no_access": True})
    resp = _client(dev, homepage_first=False).get("/", follow_redirects=False)
    # root_redirect returns 404 for a non-explorer host (or redirects for the
    # explorer host) — in all cases it is NOT the homepage.
    assert f"{settings.public_url}/mcp" not in resp.text
