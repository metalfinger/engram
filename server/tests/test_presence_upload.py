"""Presence upload — the missing limb that lets a remote machine join the roster.

Auto-presence has been single-machine by construction: the Claude Code hook writes
a spool file locally and only the server drains it, from the server's own disk. So
Hiren's Mac never appeared in the roster and its records were not queued but
stranded. The tray runs on that machine and can post what the hook wrote.
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
    token = dash.auth.issue("google:alice@example.com", "alice@example.com",
                            "alice", ttl=3600, scope="notify")
    return SimpleNamespace(client=client, registry=registry, token=token)


def _auth(env):
    return {"Authorization": f"Bearer {env.token}"}


def test_a_remote_machine_can_report_presence(env):
    r = env.client.post(
        "/dashboard/api/presence/upload",
        headers=_auth(env),
        json={"records": [{
            "session": "mac-a", "name": "Hirens-MacBook/vibechk",
            "repo": "vibechk", "branch": "m1/design", "project": "vibechk",
            "host": "Hirens-MacBook-Pro.local", "status": "working",
        }]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["ingested"] == 1


def test_it_requires_a_token(env):
    r = env.client.post("/dashboard/api/presence/upload",
                        json={"records": [{"session": "mac-a"}]})
    assert r.status_code == 401


def test_records_without_a_session_are_refused(env):
    r = env.client.post("/dashboard/api/presence/upload", headers=_auth(env),
                        json={"records": [{"name": "no session id"}]})
    assert r.status_code == 400


def test_an_empty_upload_is_refused(env):
    r = env.client.post("/dashboard/api/presence/upload", headers=_auth(env),
                        json={"records": []})
    assert r.status_code == 400


def test_a_huge_backlog_is_capped(env):
    """A machine that has been offline can hold a lot; one upload must not become
    an unbounded commit."""
    records = [{"session": f"s{i}", "project": "vibechk"} for i in range(200)]
    r = env.client.post("/dashboard/api/presence/upload", headers=_auth(env),
                        json={"records": records})
    assert r.status_code == 200
    assert r.json()["received"] == 50


def test_the_invisible_toggle_still_works(env):
    """The obvious path for this endpoint was already taken by the tray's
    invisible toggle; registering a second POST handler there would have shadowed
    it silently."""
    r = env.client.post("/dashboard/api/presence", headers=_auth(env),
                        json={"invisible": True})
    assert r.status_code != 404
