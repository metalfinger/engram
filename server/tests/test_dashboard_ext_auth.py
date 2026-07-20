"""Extension OAuth: /dashboard/ext-auth signs the Chrome extension in like everything else."""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import Dashboard, register_dashboard
from engram_server.registry import StoreRegistry

REDIR = "https://abcdefghijklmnop.chromiumapp.org/"


class FakeIdP:
    name = "google"

    def authorize_url(self, redirect_uri, state):
        return f"https://accounts.example/auth?state={state}"

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
    registry.tenancy.accept_invite(inv.token, "amiya", "a@example.com", "google", "google:a@example.com")
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, registry, {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    client = TestClient(app, base_url="https://testserver")
    session = dash.auth.issue("google:a@example.com", "a@example.com", "amiya", ttl=3600)
    return SimpleNamespace(client=client, dash=dash, session=session)


@pytest.mark.parametrize("bad", ["", "https://evil.com/", "http://x.chromiumapp.org/", "ftp://x.chromiumapp.org/"])
def test_ext_auth_rejects_bad_redirect(env, bad):
    r = env.client.get(f"/dashboard/ext-auth?redirect={bad}", follow_redirects=False)
    assert r.status_code == 400


def test_valid_ext_redirect_predicate():
    assert Dashboard._valid_ext_redirect("https://abc.chromiumapp.org/")
    assert not Dashboard._valid_ext_redirect("https://abc.example.org/")
    assert not Dashboard._valid_ext_redirect("http://abc.chromiumapp.org/")


def test_ext_auth_with_session_hands_back_notify_token(env):
    r = env.client.get(
        f"/dashboard/ext-auth?redirect={REDIR}",
        cookies={"engram_session": env.session},
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(REDIR + "#token=")
    token = loc.split("#token=", 1)[1]
    # the handed-back token is scope=notify (works on the notify API, not as a session)
    assert env.dash.auth.verify(token, expected_scope="notify") is not None
    assert env.dash.auth.verify(token, expected_scope="session") is None


def test_ext_auth_without_session_shows_signin(env):
    r = env.client.get(f"/dashboard/ext-auth?redirect={REDIR}", follow_redirects=False)
    assert r.status_code == 200
    assert "Sign in with Google" in r.text
    assert "kind=ext" in r.text
