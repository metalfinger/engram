"""v3 web IA — five-tab shell, rooms pages, team/presence API, ops view, profile polish.

Reuses the dashboard test fixture pattern from test_dashboard_browse.py /
test_dashboard_social.py (a FakeIdP + a Starlette app wrapping register_dashboard's
route table + signed session cookies minted directly via dash.auth.issue).
"""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import register_dashboard
from engram_server.registry import StoreRegistry


class FakeIdP:
    name = "google"

    def authorize_url(self, r, s): return "https://x/auth"
    async def exchange_code(self, c, r, h): return "t"
    async def fetch_user(self, t, h): return SimpleNamespace(id="x", login="a@example.com")


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
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com"), ("charlie", "charlie@example.com")):
        inv = registry.tenancy.create_invite(e)
        users[h] = registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, registry, {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    client = TestClient(app, base_url="https://testserver")
    cookies = {
        h: dash.auth.issue(f"google:{e}", e, h, ttl=3600)
        for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com"), ("charlie", "charlie@example.com"))
    }
    # settings.owner_subjects defaults to "github:metalfinger" — mint a matching
    # session so ops-view owner-only checks don't need a tenancy row at all.
    owner_cookie = dash.auth.issue("github:metalfinger", "owner@example.com", mu.owner_handle, ttl=3600)
    return SimpleNamespace(
        client=client, registry=registry, dash=dash, users=users,
        cookies=cookies, owner_cookie=owner_cookie,
    )


def _ck(env, handle):
    return {"engram_session": env.cookies[handle]}


# -- rooms -------------------------------------------------------------------

def test_rooms_list_requires_login(env):
    r = env.client.get("/dashboard/rooms", follow_redirects=False)
    assert r.status_code == 302


def test_create_room_notifies_invitee_with_ref(env):
    r = env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "design-review", "goal": "agree on the API shape", "invite": "bob"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard/rooms/design-review"
    notes = env.registry.social.list_notifications(env.users["bob"].id, unread_only=True)
    assert any(n.kind == "room_invite" and n.ref == "design-review" for n in notes)


def test_transcript_page_renders_a_posted_turn(env):
    env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "planning", "goal": "plan the launch", "invite": "bob"},
        follow_redirects=False,
    )
    post = env.client.post(
        "/dashboard/rooms/planning/post", cookies=_ck(env, "alice"),
        data={"body": "let's ship Friday"}, follow_redirects=False,
    )
    assert post.status_code == 302
    page = env.client.get("/dashboard/rooms/planning", cookies=_ck(env, "alice"), follow_redirects=False)
    assert page.status_code == 200
    assert "let&#x27;s ship Friday" in page.text or "let's ship Friday" in page.text


def test_non_member_gets_403_on_room_page_and_api(env):
    env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "private-room", "goal": "just us", "invite": "bob"},
        follow_redirects=False,
    )
    page = env.client.get("/dashboard/rooms/private-room", cookies=_ck(env, "charlie"), follow_redirects=False)
    assert page.status_code == 403
    api = env.client.get("/dashboard/api/rooms/private-room.json", cookies=_ck(env, "charlie"), follow_redirects=False)
    assert api.status_code == 403


def test_post_with_a_secret_is_refused(env):
    env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "secops", "goal": "discuss infra"}, follow_redirects=False,
    )
    r = env.client.post(
        "/dashboard/rooms/secops/post", cookies=_ck(env, "alice"),
        data={"body": "prod key is AKIA1234567890ABCDEF, don't lose it"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]
    page = env.client.get("/dashboard/rooms/secops", cookies=_ck(env, "alice"), follow_redirects=False)
    assert "AKIA1234567890ABCDEF" not in page.text


def test_close_room_then_read_only(env):
    env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "wrap-up", "goal": "wrap the project"}, follow_redirects=False,
    )
    r = env.client.post(
        "/dashboard/rooms/wrap-up/close", cookies=_ck(env, "alice"),
        data={"outcome": "shipped it"}, follow_redirects=False,
    )
    assert r.status_code == 302
    page = env.client.get("/dashboard/rooms/wrap-up", cookies=_ck(env, "alice"), follow_redirects=False)
    assert "shipped it" in page.text
    # closed rooms refuse further posts
    post = env.client.post(
        "/dashboard/rooms/wrap-up/post", cookies=_ck(env, "alice"),
        data={"body": "one more thing"}, follow_redirects=False,
    )
    assert "error=" in post.headers["location"]


def test_open_room_with_from_profile(env):
    r = env.client.post(
        "/dashboard/rooms/open-with", cookies=_ck(env, "alice"),
        data={"handle": "bob"}, follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/dashboard/rooms/alice-bob-")
    notes = env.registry.social.list_notifications(env.users["bob"].id, unread_only=True)
    assert any(n.kind == "room_invite" for n in notes)


# -- team / presence API ------------------------------------------------------

def test_api_team_shape_and_invisible_excluded(env):
    env.registry.presence.touch(env.users["alice"].id, tool="claude-code", project="engram")
    env.registry.presence.touch(env.users["bob"].id, tool="claude-code", project="engram")
    env.registry.presence.set_invisible(env.users["bob"].id, True)
    r = env.client.get("/dashboard/api/team", cookies=_ck(env, "alice"), follow_redirects=False)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    handles = {t["handle"] for t in data["team"]}
    assert "alice" in handles
    assert "bob" not in handles
    assert data["me"]["handle"] == "alice"
    assert data["me"]["invisible"] is False


def test_api_presence_toggle(env):
    r = env.client.post(
        "/dashboard/api/presence", cookies=_ck(env, "alice"),
        json={"invisible": True}, follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json()["invisible"] is True
    row = env.registry.presence.self_row(env.users["alice"].id)
    assert row is not None and row["invisible"] is True


# -- notifications API ---------------------------------------------------------

def test_api_notifications_rows_carry_ref(env):
    env.registry.social.create_notification(env.users["alice"].id, "question", "asked something", ref="42")
    token = env.dash.auth.issue("google:alice@example.com", "alice@example.com", "alice",
                                ttl=3600, scope="notify")
    r = env.client.get("/dashboard/api/notifications", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["unread"] and data["unread"][0]["ref"] == "42"


# -- ops -----------------------------------------------------------------------

def test_ops_403_for_non_owner_and_200_for_owner(env):
    r = env.client.get("/dashboard/ops", cookies=_ck(env, "alice"), follow_redirects=False)
    assert r.status_code == 403
    r = env.client.get("/dashboard/ops", cookies={"engram_session": env.owner_cookie}, follow_redirects=False)
    assert r.status_code == 200
    assert "alice" in r.text


# -- profile polish --------------------------------------------------------------

def test_avatar_oversize_rejected(env):
    huge = "data:image/jpeg;base64," + ("A" * 100_001)
    r = env.client.post(
        "/dashboard/profile", cookies=_ck(env, "alice"),
        data={"display_name": "Alice", "avatar_url": huge}, follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Avatar must be" in r.text


# -- setup + artifacts parity pages ----------------------------------------------

def test_setup_page_mentions_kb_import(env):
    r = env.client.get("/dashboard/setup", cookies=_ck(env, "alice"), follow_redirects=False)
    assert r.status_code == 200
    assert "kb_import" in r.text


def test_artifacts_page_200(env):
    r = env.client.get("/dashboard/artifacts", cookies=_ck(env, "alice"), follow_redirects=False)
    assert r.status_code == 200


# -- sec-review: same-origin guard on state-changing POSTs -------------------


def test_cross_subdomain_post_is_refused(env, settings):
    """The domain-wide cookie is same-SITE for sibling subdomains, so SameSite=Lax
    does not stop them — the Origin check must."""
    resp = env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "evil-room", "goal": "csrf"},
        headers={"Origin": "https://evil.metalfinger.xyz"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert "Cross-origin" in resp.text
    # and nothing was created
    assert env.registry.rooms.room_by_name("evil-room") is None


def test_own_origins_and_no_origin_still_pass(env, settings):
    ok = env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "good-room", "goal": "a real goal"},
        headers={"Origin": settings.public_url},
        follow_redirects=False,
    )
    assert ok.status_code == 302
    no_origin = env.client.post(
        "/dashboard/rooms", cookies=_ck(env, "alice"),
        data={"name": "curl-room", "goal": "another goal"},
        follow_redirects=False,
    )
    assert no_origin.status_code == 302


# -- desktop tray app: loopback OAuth redirect --------------------------------


def test_loopback_redirect_allowed_for_desktop_app(env):
    from engram_server.dashboard import Dashboard

    ok = Dashboard._valid_ext_redirect
    assert ok("http://127.0.0.1:53123/callback/abc123XYZ_-") is True
    assert ok("https://abcdef.chromiumapp.org/x") is True  # extension unchanged
    # everything else refused
    assert ok("http://127.0.0.1:53123/steal") is False           # wrong path shape
    assert ok("http://127.0.0.1:53123/callback/x") is False      # nonce too short
    assert ok("http://192.168.1.5:53123/callback/abc123XYZ_") is False  # not loopback
    assert ok("https://evil.example/callback/abc123XYZ_") is False
    assert ok("http://localhost:53123/callback/abc123XYZ_") is False  # IP literal only (RFC 8252)


# -- live notification delivery (?wait / ?since long-poll) --------------------


def test_notifications_longpoll_returns_instantly_when_new_exists(env):
    import time

    env.registry.social.create_notification(env.users["alice"].id, "dm", "hi", "1")
    tok = env.dash.auth.issue("google:alice@example.com", "alice@example.com", "alice",
                              ttl=3600, scope="notify")
    t0 = time.monotonic()
    r = env.client.get("/dashboard/api/notifications?wait=30&since=0",
                       headers={"Authorization": f"Bearer {tok}"})
    took = time.monotonic() - t0
    assert r.status_code == 200 and r.json()["unread"]
    assert took < 2, f"should not park when something newer than since exists ({took:.1f}s)"


def test_notifications_longpoll_parks_then_times_out_on_stale_cursor(env):
    import time

    note = env.registry.social.create_notification(env.users["alice"].id, "dm", "old", "1")
    tok = env.dash.auth.issue("google:alice@example.com", "alice@example.com", "alice",
                              ttl=3600, scope="notify")
    t0 = time.monotonic()
    r = env.client.get(f"/dashboard/api/notifications?wait=1&since={note.id}",
                       headers={"Authorization": f"Bearer {tok}"})
    took = time.monotonic() - t0
    assert r.status_code == 200
    assert took >= 0.9, f"should have parked ~1s on an up-to-date cursor ({took:.2f}s)"


# -- folder filing on the web -------------------------------------------------


@pytest.mark.asyncio
async def test_move_project_to_folder_from_the_web(env):
    store = await env.registry.store_for_handle("alice")
    await store.kb_write("projects/webmove/context.md",
                         "---\ntype: project\ndescription: wm\n---\n\n# About\n\nX.\n", "seed")
    r = env.client.post("/dashboard/p/webmove/move", cookies=_ck(env, "alice"),
                        data={"folder": "personal"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard/p/webmove"
    assert (store.root / "projects/personal/webmove/context.md").is_file()
    # the project PAGE resolves at its new foldered home (regression: used to 404)
    page = env.client.get("/dashboard/p/webmove", cookies=_ck(env, "alice"), follow_redirects=False)
    assert page.status_code == 200
    assert "personal" in page.text  # folder shown in the filing control
