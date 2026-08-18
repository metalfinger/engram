"""Answering ask_human from outside a session.

ask_human reaches Hiren well; ANSWERING it has meant opening a page he rarely
opens. These endpoints are the read and write halves of closing that loop from
the tray — the one surface alive when no session is. They mirror
kb_room_relay_answer, which does the same job from inside a session; both exist
because he is reachable in two places and should answer from whichever he is in.
"""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

import engram_server.app as app_module
from engram_server.dashboard import register_dashboard
from engram_server.registry import StoreRegistry


class FakeIdP:
    name = "google"

    def authorize_url(self, r, s): return "https://x/auth"
    async def exchange_code(self, c, r, h): return "t"
    async def fetch_user(self, t, h): return SimpleNamespace(id="x", login="a@example.com")


@pytest.fixture()
def env(settings, tmp_path, monkeypatch):
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
    # the MCP side, so we can create the block the way a session really would
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", mu)
    monkeypatch.setattr(app_module, "_presence_last", {})
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="google:alice@example.com"))
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    return SimpleNamespace(client=client, registry=registry, token=token)


def _auth(env):
    return {"Authorization": f"Bearer {env.token}"}


@pytest.mark.asyncio
async def test_a_blocked_room_appears_and_can_be_answered(env):
    await app_module.kb_room_open("decide", "needs a person")
    await app_module.kb_room_post("decide", "stuck", speaker="windows",
                                  ask_human="Ship it or hold?")

    asks = env.client.get("/dashboard/api/asks", headers=_auth(env)).json()
    assert [a["name"] for a in asks["asks"]] == ["decide"]
    assert asks["asks"][0]["kind"] == "room"
    assert asks["asks"][0]["question"] == "Ship it or hold?"

    r = env.client.post("/dashboard/api/asks/answer", headers=_auth(env),
                        json={"name": "decide", "answer": "Hold it."})
    assert r.status_code == 200 and r.json()["kind"] == "room"
    after = env.client.get("/dashboard/api/asks", headers=_auth(env)).json()
    assert after["asks"] == [], "answering must clear the block"


@pytest.mark.asyncio
async def test_a_blocked_thread_shows_as_a_thread_and_clears(env):
    """A thread keeps its transcript in git, so nothing passes through post_turn
    to notice the answer — the unblock has to be explicit."""
    await app_module.kb_thread_post("handover", "windows", "stuck",
                                    ask_human="Flag or hold?")
    asks = env.client.get("/dashboard/api/asks", headers=_auth(env)).json()
    assert asks["asks"][0]["name"] == "handover", "the shadow prefix must not leak"
    assert asks["asks"][0]["kind"] == "thread"

    r = env.client.post("/dashboard/api/asks/answer", headers=_auth(env),
                        json={"name": "handover", "answer": "Hold for now."})
    assert r.status_code == 200 and r.json()["kind"] == "thread"

    read = await app_module.kb_thread_read("handover", sender="windows")
    assert read["floor"]["awaiting_human"] == ""
    assert any("Hold for now." in t["message"] for t in read["turns"])


def test_answering_requires_a_token(env):
    r = env.client.post("/dashboard/api/asks/answer",
                        json={"name": "x", "answer": "y"})
    assert r.status_code == 401


def test_an_unknown_name_is_a_404_not_a_silent_success(env):
    r = env.client.post("/dashboard/api/asks/answer", headers=_auth(env),
                        json={"name": "does-not-exist", "answer": "hi"})
    assert r.status_code == 404


def test_an_empty_answer_is_refused(env):
    r = env.client.post("/dashboard/api/asks/answer", headers=_auth(env),
                        json={"name": "decide", "answer": "   "})
    assert r.status_code == 400


def test_nothing_pending_is_an_empty_list_not_an_error(env):
    r = env.client.get("/dashboard/api/asks", headers=_auth(env))
    assert r.status_code == 200 and r.json()["asks"] == []
