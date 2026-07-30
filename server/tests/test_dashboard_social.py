"""M2.4 — dashboard social panel: contacts (add/accept) + notifications in the browser."""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import register_dashboard
from engram_server.registry import StoreRegistry


class FakeIdP:
    name = "google"

    def authorize_url(self, r, s):
        return "https://x/auth"

    async def exchange_code(self, c, r, h):
        return "t"

    async def fetch_user(self, t, h):
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
    users = {}
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        users[h] = registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, registry, {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    client = TestClient(app, base_url="https://testserver")
    alice_cookie = dash.auth.issue("google:alice@example.com", "alice@example.com", "alice", ttl=3600)
    bob_cookie = dash.auth.issue("google:bob@example.com", "bob@example.com", "bob", ttl=3600)
    return SimpleNamespace(client=client, registry=registry, users=users,
                           alice=alice_cookie, bob=bob_cookie)


def test_add_and_accept_contact_via_dashboard(env):
    c = env.client
    # alice adds bob
    r = c.post("/dashboard/contact/add", cookies={"engram_session": env.alice},
               data={"handle": "bob"}, follow_redirects=False)
    assert r.status_code == 200 and "Request sent to @bob" in r.text
    # bob sees a notification + an incoming request on his dashboard
    page = c.get("/dashboard", cookies={"engram_session": env.bob}, follow_redirects=False)
    assert "wants to connect" in page.text
    assert "/dashboard/contact/accept" in page.text
    # bob accepts
    r = c.post("/dashboard/contact/accept", cookies={"engram_session": env.bob},
               data={"handle": "alice"}, follow_redirects=False)
    assert "connected with @alice" in r.text
    assert env.registry.social.are_contacts(env.users["alice"].id, env.users["bob"].id)


def test_mark_notifications_read(env):
    env.registry.social.create_notification(env.users["bob"].id, "dm", "@alice: hi")
    c = env.client
    page = c.get("/dashboard", cookies={"engram_session": env.bob}, follow_redirects=False)
    assert "@alice: hi" in page.text
    c.post("/dashboard/notifications/read", cookies={"engram_session": env.bob}, follow_redirects=False)
    assert env.registry.social.unread_counts(env.users["bob"].id)["notifications"] == 0


@pytest.mark.parametrize(
    "referer",
    [
        "https://evil.com/dashboard",        # substring check would have passed this
        "//evil.com/dashboard",              # protocol-relative
        "https://evil.com/dashboard/people",
        "http://testserver.evil.com/dashboard",
    ],
)
def test_follow_never_redirects_off_site(env, referer):
    """Open-redirect guard: the Referer is attacker-influenceable, so a cross-origin
    one must never become the redirect target."""
    r = env.client.post(
        "/dashboard/follow",
        cookies={"engram_session": env.alice},
        data={"handle": "bob", "follow": "1"},
        headers={"referer": referer},
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    assert "evil.com" not in location
    assert location.startswith("/dashboard")


def test_follow_returns_to_the_same_site_page(env):
    """The UX the guard must preserve: following from a profile returns to that profile."""
    r = env.client.post(
        "/dashboard/follow",
        cookies={"engram_session": env.alice},
        data={"handle": "bob", "follow": "1"},
        headers={"referer": "https://testserver/dashboard/u/bob"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/dashboard/u/bob"


def test_social_panel_requires_session(env):
    r = env.client.post("/dashboard/contact/add", data={"handle": "bob"}, follow_redirects=False)
    assert r.status_code == 302  # bounced to login
