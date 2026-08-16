"""Item 3 — the human's view of who is working where.

Sessions have carried this in their `floor` block since the coordination wave.
Hiren had no view of it short of asking one of them — and he is the one
participant who cannot be handed a tool result, so the page he actually opens
has to carry it.
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
    inv = registry.tenancy.create_invite("alice@example.com")
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "google", "google:alice@example.com")
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, registry, {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    client = TestClient(app, base_url="https://testserver")
    cookie = dash.auth.issue("google:alice@example.com", "alice@example.com",
                             "alice", ttl=3600)
    return SimpleNamespace(client=client, registry=registry, dash=dash,
                           cookies={"engram_session": cookie})


@pytest.mark.asyncio
async def test_a_claim_shows_on_the_office_page(env):
    store = await env.registry.store_for_handle("alice")
    await store.kb_claim("mac-a", "projects/vibechk/specs/theme.md", "reskin")
    r = env.client.get("/dashboard/office", cookies=env.cookies)
    assert r.status_code == 200
    assert "Working on" in r.text
    assert "projects/vibechk/specs/theme.md" in r.text
    assert "mac-a" in r.text
    assert "claimed" in r.text


def test_derived_activity_shows_too(env):
    uid = env.registry.tenancy.user_by_handle("alice").id
    env.registry.rooms.record_activity(uid, "sess-b", "server/app.py", label="mac-b")
    r = env.client.get("/dashboard/office", cookies=env.cookies)
    assert "server/app.py" in r.text
    assert "writing" in r.text
    assert "mac-b" in r.text


def test_a_quiet_workspace_shows_no_panel(env):
    r = env.client.get("/dashboard/office", cookies=env.cookies)
    assert r.status_code == 200
    assert "Working on" not in r.text


def test_shadow_rooms_never_appear_as_rooms(env):
    """Thread floor state lives in a hidden room. Listing it would show a phantom
    room beside every thread — the MCP kb_rooms already filters these, and the
    dashboard was reading the same table unfiltered."""
    uid = env.registry.tenancy.user_by_handle("alice").id
    env.registry.rooms.open_room(uid, "thread--vibechk-handoff", "floor state")
    env.registry.rooms.open_room(uid, "real-room", "an actual conversation")
    r = env.client.get("/dashboard/office", cookies=env.cookies)
    assert "real-room" in r.text
    assert "thread--vibechk-handoff" not in r.text
