"""M4 — per-user brain browser in the unified home (scoped to the caller's OWN brain)."""

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
    cookies = {}
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, registry, {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    client = TestClient(app, base_url="https://testserver")
    cookies["alice"] = dash.auth.issue("google:alice@example.com", "alice@example.com", "alice", ttl=3600)
    cookies["bob"] = dash.auth.issue("google:bob@example.com", "bob@example.com", "bob", ttl=3600)
    return SimpleNamespace(client=client, registry=registry, cookies=cookies)


async def _write(registry, handle, subject, path, desc, body):
    store = await registry.store_for_handle(handle)
    await store.kb_write(path, f"---\ntype: project\ndescription: {desc}\n---\n\n# About\n\n{body}\n", "seed")


@pytest.mark.asyncio
async def test_home_lists_own_projects_and_browses_concept(env):
    await _write(env.registry, "alice", "google:alice@example.com",
                 "projects/alt/context.md", "alt project", "Alice's alt plan here")
    c = env.client
    # home shows alice's project
    home = c.get("/dashboard", cookies={"engram_session": env.cookies["alice"]}, follow_redirects=False)
    assert home.status_code == 200
    assert "Your projects" in home.text and "/dashboard/p/alt" in home.text
    # project page renders context + links to the concept
    proj = c.get("/dashboard/p/alt", cookies={"engram_session": env.cookies["alice"]}, follow_redirects=False)
    assert proj.status_code == 200
    assert "Alice&#x27;s alt plan here" in proj.text or "Alice's alt plan here" in proj.text
    # concept page renders
    concept = c.get("/dashboard/f/projects/alt/context.md",
                    cookies={"engram_session": env.cookies["alice"]}, follow_redirects=False)
    assert concept.status_code == 200
    assert "alt plan here" in concept.text


@pytest.mark.asyncio
async def test_cannot_browse_another_users_brain(env):
    await _write(env.registry, "alice", "google:alice@example.com",
                 "projects/secret/context.md", "secret", "ALICE PRIVATE")
    c = env.client
    # bob (logged in as bob) hitting the same path reads BOB's brain, not alice's
    r = c.get("/dashboard/f/projects/secret/context.md",
              cookies={"engram_session": env.cookies["bob"]}, follow_redirects=False)
    assert "ALICE PRIVATE" not in r.text  # bob has no such file -> 404, never alice's content
    assert r.status_code == 404
    # bob's home shows no projects (his brain is empty)
    home = c.get("/dashboard", cookies={"engram_session": env.cookies["bob"]}, follow_redirects=False)
    assert "No projects yet" in home.text


@pytest.mark.asyncio
async def test_traversal_and_auth(env):
    c = env.client
    # path traversal is rejected by the store's resolver -> 404, never escapes
    r = c.get("/dashboard/f/../../etc/passwd",
              cookies={"engram_session": env.cookies["alice"]}, follow_redirects=False)
    assert r.status_code in (404, 400)
    # no session -> bounced to login
    assert c.get("/dashboard/p/alt", follow_redirects=False).status_code == 302
    assert c.get("/dashboard/f/projects/alt/context.md", follow_redirects=False).status_code == 302
