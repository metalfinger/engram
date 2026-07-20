"""M1.4 security — OAuth login-CSRF / session-fixation: state must be bound to the browser."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import OAUTH_STATE_COOKIE, register_dashboard
from engram_server.registry import StoreRegistry


class FakeIdP:
    name = "google"

    def authorize_url(self, redirect_uri, state):
        return f"https://accounts.example/auth?state={state}"

    async def exchange_code(self, code, redirect_uri, http):
        return "upstream-token"

    async def fetch_user(self, token, http):
        return SimpleNamespace(id="x", login="a@example.com")


@pytest.fixture()
def client(settings, tmp_path):
    mu = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
        "dashboard_session_secret": "x" * 40,
    })
    mcp = FastMCP("test-dash")
    register_dashboard(mcp, mu, StoreRegistry(mu), {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    # https base_url so the Secure state cookie is retained + resent (prod is always
    # https behind cloudflared; over http a Secure cookie would silently drop).
    return TestClient(app, base_url="https://testserver")


def _start_flow(client):
    r = client.get("/dashboard/auth/google?kind=login", follow_redirects=False)
    assert r.status_code == 302
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    assert OAUTH_STATE_COOKIE in r.headers.get("set-cookie", "")
    return state


def test_callback_rejects_missing_state_cookie(client):
    state = _start_flow(client)
    client.cookies.clear()  # simulate a different browser (no state cookie)
    r = client.get(f"/dashboard/callback?state={state}&code=abc", follow_redirects=False)
    assert r.status_code == 400
    assert "could not be verified" in r.text


def test_callback_rejects_mismatched_state(client):
    _start_flow(client)  # sets a cookie for one state
    r = client.get("/dashboard/callback?state=attacker-supplied&code=abc", follow_redirects=False)
    assert r.status_code == 400


def test_callback_accepts_matching_state_and_continues(client):
    """With the matching cookie the CSRF check passes; the flow then continues to the
    'no account yet' page (this login has no account) — proving binding didn't block a
    legitimate flow."""
    state = _start_flow(client)  # TestClient retains the Set-Cookie automatically
    r = client.get(f"/dashboard/callback?state={state}&code=abc", follow_redirects=False)
    assert r.status_code == 403
    assert "No account" in r.text
