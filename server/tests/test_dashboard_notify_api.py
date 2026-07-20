"""M3 — the bearer-token notification API the Chrome extension polls."""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import register_dashboard
from engram_server.registry import StoreRegistry


class FakeIdP:
    name = "google"

    def authorize_url(self, redirect_uri, state):
        return "https://accounts.example/auth"

    async def exchange_code(self, code, redirect_uri, http):
        return "t"

    async def fetch_user(self, token, http):
        return SimpleNamespace(id="x", login="a@example.com")


@pytest.fixture()
def env(settings, tmp_path):
    mu = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
        "dashboard_session_secret": "z" * 40,
    })
    registry = StoreRegistry(mu)
    inv = registry.tenancy.create_invite("a@example.com")
    user = registry.tenancy.accept_invite(inv.token, "amiya", "a@example.com", "google", "google:a@example.com")
    mcp = FastMCP("test-dash")
    dash = register_dashboard(mcp, mu, registry, {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    client = TestClient(app, base_url="https://testserver")
    token = dash.auth.issue("google:a@example.com", "a@example.com", "amiya", ttl=3600, scope="notify")
    session_token = dash.auth.issue("google:a@example.com", "a@example.com", "amiya", ttl=3600)
    return SimpleNamespace(client=client, registry=registry, user=user, token=token, session_token=session_token)


def test_api_requires_valid_bearer(env):
    assert env.client.get("/dashboard/api/notifications").status_code == 401
    assert env.client.get(
        "/dashboard/api/notifications", headers={"Authorization": "Bearer garbage"}
    ).status_code == 401


def test_api_returns_unread_and_marks_read(env):
    env.registry.social.create_notification(env.user.id, "dm", "@bob: hi")
    env.registry.social.create_notification(env.user.id, "contact_request", "@bob wants to connect")
    h = {"Authorization": f"Bearer {env.token}"}

    r = env.client.get("/dashboard/api/notifications", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and len(data["unread"]) == 2
    assert data["counts"]["notifications"] == 2
    assert {n["kind"] for n in data["unread"]} == {"dm", "contact_request"}

    assert env.client.post("/dashboard/api/notifications/read", headers=h).json()["ok"]
    assert env.client.get("/dashboard/api/notifications", headers=h).json()["unread"] == []


def test_dashboard_page_shows_extension_token(env):
    # a real (session-scoped) cookie renders the page + the bearer token block
    html = env.client.get(
        "/dashboard",
        cookies={"engram_session": env.session_token},
        follow_redirects=False,
    )
    assert html.status_code == 200
    assert "Desktop notifications" in html.text


def test_notify_token_cannot_be_replayed_as_session(env):
    # the extension (scope=notify) token must NOT open a dashboard session
    r = env.client.get(
        "/dashboard", cookies={"engram_session": env.token}, follow_redirects=False
    )
    assert r.status_code == 302  # bounced to /dashboard/login, not authorized


def test_session_token_cannot_be_used_on_the_notify_api(env):
    # a session-scoped token must NOT authenticate the extension API
    r = env.client.get(
        "/dashboard/api/notifications",
        headers={"Authorization": f"Bearer {env.session_token}"},
    )
    assert r.status_code == 401
