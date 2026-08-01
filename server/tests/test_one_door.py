"""v3 Wave 1 "One Door" — the explorer guard authenticates with the DASHBOARD
session cookie, not Cloudflare Access.

The whole point of the wave: one identity system. These tests pin the three
outcomes of the new guard (anonymous -> login, teammate -> their dashboard,
owner -> the brain) plus the two fallbacks (dev bypass; legacy CF path when no
dashboard secret exists, i.e. a pre-account single-user deployment).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import SESSION_COOKIE, DashboardAuth
from engram_server.explorer import register as register_explorer

SECRET = "x" * 32


def _client(settings) -> TestClient:
    mcp = FastMCP("test-one-door")
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


def _one_door(settings):
    return settings.model_copy(
        update={
            "dev_no_access": False,
            "dashboard_session_secret": SECRET,
            "owner_subjects": "github:metalfinger",
        }
    )


def test_anonymous_is_sent_to_login(settings):
    resp = _client(_one_door(settings)).get("/brain", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/login"


def test_teammate_is_sent_to_their_own_dashboard(settings):
    """A signed-in NON-owner never sees /brain/* — it renders the owner's whole
    brain. Their surface is the per-user dashboard."""
    s = _one_door(settings)
    auth = DashboardAuth(SECRET, 3600)
    cookie = auth.issue("github:teammate", "t@example.com", "teammate")
    resp = _client(s).get("/brain", cookies={SESSION_COOKIE: cookie}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"


def test_owner_session_opens_the_brain(settings):
    s = _one_door(settings)
    auth = DashboardAuth(SECRET, 3600)
    cookie = auth.issue("github:metalfinger", "hir.012612@gmail.com", "hiren")
    resp = _client(s).get("/brain", cookies={SESSION_COOKIE: cookie}, follow_redirects=False)
    assert resp.status_code == 200


def test_notify_scoped_token_is_not_a_session(settings):
    """The extension's long-lived scope='notify' token must NOT open the brain —
    the scope check is what stops replay escalation."""
    s = _one_door(settings)
    auth = DashboardAuth(SECRET, 3600)
    cookie = auth.issue("github:metalfinger", "hir.012612@gmail.com", "hiren", scope="notify")
    resp = _client(s).get("/brain", cookies={SESSION_COOKIE: cookie}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/login"


def test_garbage_cookie_is_anonymous(settings):
    s = _one_door(settings)
    resp = _client(s).get(
        "/brain", cookies={SESSION_COOKIE: "not-a-jwt"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/login"


def test_no_secret_falls_back_to_cf_access(settings):
    """A single-user deployment that never set a dashboard secret keeps the old
    Cloudflare Access wall (403, teaching message) — not an open door."""
    s = settings.model_copy(
        update={
            "dev_no_access": False,
            "dashboard_session_secret": "",
            "cf_access_team_domain": "example.cloudflareaccess.com",
            "cf_access_aud": "aud",
        }
    )
    resp = _client(s).get("/brain", follow_redirects=False)
    assert resp.status_code == 403
    assert "Cloudflare Access" in resp.text


def test_dev_bypass_still_works(settings):
    dev = settings.model_copy(update={"dev_no_access": True})
    resp = _client(dev).get("/brain")
    assert resp.status_code == 200
