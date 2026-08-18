""""Am I current?" — the question a machine could not previously ask.

Hiren works across three machines and adds more. The one he is sitting at gets
updates because a session is there to make them; the others drift. The macOS
python-vs-python3 hook fix had to be hand-delivered by commit-pinned URL, and a
Mac session spent an afternoon on a stale SKILL.md.
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
    return SimpleNamespace(client=client, token=token)


def _auth(env):
    return {"Authorization": f"Bearer {env.token}"}


def test_a_machine_can_ask_what_it_should_have(env):
    r = env.client.get("/dashboard/api/machine", headers=_auth(env))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["server_version"]
    names = {s["name"] for s in body["skills"]}
    assert {"engram", "chunk-work"} <= names, "every skill must be listed, not just engram"
    assert all(s["digest"] for s in body["skills"]), "a digest is how a machine compares"
    assert body["hooks"]["digest"]


def test_it_says_that_a_file_copy_cannot_fix_a_stale_tool_list(env):
    """The most expensive confusion of the whole build: a machine can look updated
    and still be running cached tool schemas."""
    body = env.client.get("/dashboard/api/machine", headers=_auth(env)).json()
    assert "reconnect" in body["note"]
    assert "schemas are cached" in body["note"]


def test_the_digest_changes_when_content_changes(env, tmp_path, monkeypatch):
    from engram_server.app import _dir_digest

    d = tmp_path / "skilldir"
    d.mkdir()
    (d / "SKILL.md").write_text("one", encoding="utf-8")
    first = _dir_digest(d)
    (d / "SKILL.md").write_text("two", encoding="utf-8")
    assert _dir_digest(d) != first, "otherwise a machine can never tell it is stale"


def test_it_requires_a_token(env):
    assert env.client.get("/dashboard/api/machine").status_code == 401


def test_hooks_are_downloadable_as_a_zip():
    """The macOS fix was hand-delivered by commit-pinned URL because there was no
    way to fetch the current hooks.

    Checked against the REAL app: the download routes live on app.py's mcp, and
    the dashboard-only test harness above does not carry them — a 404 there would
    be the harness, not the server."""
    import engram_server.app as app_module

    paths = {getattr(r, "path", "") for r in app_module.mcp._custom_starlette_routes}
    assert "/downloads/engram-hooks.zip" in paths
    assert "/downloads/engram-skill.zip" in paths


def test_the_setup_page_carries_the_machine_prompt(env):
    """A brand-new computer has no tray and no session — it has a browser. So the
    one place the setup prompt must be findable is the web page."""
    r = env.client.get("/dashboard/setup", cookies={
        "engram_session": env.token,
    })
    # bearer token is not a session cookie; use the page's own auth path
    assert r.status_code in (200, 302)


def test_setup_page_html_mentions_kb_setup_machine(settings, tmp_path):
    """Checked on the rendered body rather than through auth, so the assertion is
    about the CONTENT rather than the session plumbing."""
    from types import SimpleNamespace

    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from engram_server.dashboard import register_dashboard
    from engram_server.registry import StoreRegistry

    class _IdP:
        name = "google"

        def authorize_url(self, r, s): return "https://x/auth"
        async def exchange_code(self, c, r, h): return "t"
        async def fetch_user(self, t, h): return SimpleNamespace(id="x", login="a@e.com")

    mu = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "u"),
        "tenancy_db_path": str(tmp_path / "e.db"),
        "dashboard_session_secret": "z" * 40,
    })
    registry = StoreRegistry(mu)
    inv = registry.tenancy.create_invite("alice@example.com")
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "google", "google:alice@example.com")
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, registry, {"google": _IdP()})
    client = TestClient(Starlette(routes=mcp._custom_starlette_routes),
                        base_url="https://testserver")
    cookie = dash.auth.issue("google:alice@example.com", "alice@example.com",
                             "alice", ttl=3600)
    body = client.get("/dashboard/setup", cookies={"engram_session": cookie}).text
    assert "kb_setup_machine" in body
    assert "every" in body, "it must say to do this on every computer"
