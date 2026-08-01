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


# -- one-door session cookie scope -----------------------------------------


def _dash(settings):
    """A Dashboard wired enough to test _cookie_domain (no routes needed)."""
    from engram_server.dashboard import Dashboard
    from engram_server.registry import StoreRegistry

    s = settings.model_copy(update={"dashboard_session_secret": SECRET})
    return Dashboard(s, StoreRegistry(s), idps={})


def test_session_cookie_spans_both_production_hostnames(settings):
    """The OAuth callback lands on engram.*, humans live on brain.* — the cookie
    must be parent-domain-scoped or brain.* stays logged out forever."""
    d = _dash(settings.model_copy(update={
        "public_url": "https://engram.metalfinger.xyz",
        "explorer_url": "https://brain.metalfinger.xyz",
    }))
    assert d._cookie_domain() == ".metalfinger.xyz"


def test_session_cookie_stays_host_only_when_single_host(settings):
    d = _dash(settings.model_copy(update={
        "public_url": "https://engram.example.com",
        "explorer_url": "https://engram.example.com",
    }))
    assert d._cookie_domain() is None


def test_session_cookie_stays_host_only_across_unrelated_domains(settings):
    d = _dash(settings.model_copy(update={
        "public_url": "https://a.example.com",
        "explorer_url": "https://b.other.net",
    }))
    assert d._cookie_domain() is None


def test_oauth_state_cookie_spans_both_hostnames(settings):
    """The flow STARTS on brain.*, the callback LANDS on engram.* — a host-only
    state cookie never arrives and every brain.*-initiated sign-in dies with
    'could not be verified'. (Field bug, 2026-08-01.)"""
    from engram_server.dashboard import Dashboard
    from engram_server.registry import StoreRegistry

    class FakeIdP:
        name = "google"
        def authorize_url(self, r, s): return f"https://idp.example/auth?state={s}"

    s = settings.model_copy(update={
        "multiuser": True,
        "dashboard_session_secret": SECRET,
        "public_url": "https://engram.metalfinger.xyz",
        "explorer_url": "https://brain.metalfinger.xyz",
    })
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from engram_server.dashboard import register_dashboard

    mcp = FastMCP("od")
    register_dashboard(mcp, s, StoreRegistry(s), {"google": FakeIdP()})
    client = TestClient(Starlette(routes=mcp._custom_starlette_routes),
                        base_url="https://brain.metalfinger.xyz")
    r = client.get("/dashboard/auth/google", follow_redirects=False)
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert "engram_oauth_state" in set_cookie
    assert "Domain=.metalfinger.xyz" in set_cookie or "domain=.metalfinger.xyz" in set_cookie


def test_multiuser_registers_no_explorer_pages(settings):
    """ONE WEB APP: with human_pages=False (multi-user prod) the explorer's pages
    and APIs do not exist — no redirects, no second UI. Only /share/* survives."""
    mcp = FastMCP("test-one-web")
    register_explorer(mcp, settings.model_copy(update={"dev_no_access": True}),
                      human_pages=False)
    paths = sorted({r.path for r in mcp._custom_starlette_routes})
    assert paths == ["/share/{token}"], paths


def test_single_user_keeps_the_full_explorer(settings):
    mcp = FastMCP("test-single")
    register_explorer(mcp, settings.model_copy(update={"dev_no_access": True}),
                      human_pages=True)
    paths = {r.path for r in mcp._custom_starlette_routes}
    assert "/brain" in paths and "/brain/office" in paths and "/share/{token}" in paths
