"""The public marketing homepage (``GET /``) — homepage.py.

Zero-network: homepage_html() is pure string rendering, and the registration
check spins up a bare FastMCP carrying ONLY register_homepage (not the full
explorer) so it can't collide with the guarded ``/`` redirect that
routes.py registers separately.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.config import Settings
from engram_server.explorer.homepage import homepage_html, register_homepage

SETTINGS = Settings(public_url="https://engram.metalfinger.xyz")


def _client(settings: Settings) -> TestClient:
    mcp = FastMCP("test-homepage")
    register_homepage(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


# ------------------------------------------------------------ homepage_html


def test_contains_mcp_url() -> None:
    html = homepage_html(SETTINGS)
    assert "https://engram.metalfinger.xyz/mcp" in html


def test_contains_all_three_clients() -> None:
    html = homepage_html(SETTINGS)
    assert "claude.ai" in html
    assert "Claude Code" in html
    assert "ChatGPT" in html


def test_mentions_invite() -> None:
    html = homepage_html(SETTINGS)
    assert "invite" in html.lower()


def test_links_to_dashboard() -> None:
    html = homepage_html(SETTINGS)
    assert "https://engram.metalfinger.xyz/dashboard" in html


def test_claude_code_install_command_present() -> None:
    html = homepage_html(SETTINGS)
    assert "claude mcp add --transport http engram https://engram.metalfinger.xyz/mcp" in html


def test_structurally_valid_html() -> None:
    html = homepage_html(SETTINGS)
    stripped = html.strip()
    assert stripped.lower().startswith("<!doctype html>")
    assert "<html" in html and html.rstrip().endswith("</html>")
    assert html.count("<head>") == 1 and html.count("</head>") == 1
    assert html.count("<body>") == 1 and html.count("</body>") == 1
    assert html.count("<style>") == html.count("</style>")


def test_self_contained_no_external_requests() -> None:
    html = homepage_html(SETTINGS)
    lowered = html.lower()
    assert "cdn" not in lowered
    assert "googleapis" not in lowered
    assert "<script src=" not in lowered
    assert "http://" not in html  # no non-https external references either
    # No external stylesheet or font links.
    assert "<link" not in lowered or "stylesheet" not in lowered


def test_different_public_url_reflected() -> None:
    settings = Settings(public_url="https://example.test")
    html = homepage_html(settings)
    assert "https://example.test/mcp" in html
    assert "https://engram.metalfinger.xyz" not in html


def test_varies_public_url_used_in_dashboard_link() -> None:
    settings = Settings(public_url="https://example.test/")  # trailing slash
    html = homepage_html(settings)
    assert "https://example.test/dashboard" in html
    assert "https://example.test//dashboard" not in html


# ------------------------------------------------------------ registration


def test_register_homepage_serves_html_at_root() -> None:
    client = _client(SETTINGS)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Engram" in resp.text
    assert "https://engram.metalfinger.xyz/mcp" in resp.text


def test_register_homepage_is_unguarded() -> None:
    # No Cloudflare Access / OAuth guard applied — the route must be reachable
    # with no auth headers and no dev_no_access override.
    settings = Settings(public_url="https://engram.metalfinger.xyz", dev_no_access=False)
    client = _client(settings)
    resp = client.get("/")
    assert resp.status_code == 200
