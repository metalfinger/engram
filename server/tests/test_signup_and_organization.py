"""Open signup, project organization (tags/status), and visibility marking."""

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import Dashboard, register_dashboard
from engram_server.kbstore import KBStore
from engram_server.registry import StoreRegistry
from engram_server.tenancy import TenancyError


class FakeIdP:
    name = "google"

    def authorize_url(self, r, s):
        return f"https://accounts.example/auth?state={s}"

    async def exchange_code(self, c, r, h):
        return "t"

    async def fetch_user(self, t, h):
        return SimpleNamespace(id="x", login="newcomer@example.com")


def _mu(settings, tmp_path, **over):
    return settings.model_copy(update={
        "multiuser": True, "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
        "dashboard_session_secret": "z" * 40, **over,
    })


# -- open signup ------------------------------------------------------------


def test_open_signup_creates_an_account_without_an_invite(settings, tmp_path):
    reg = StoreRegistry(_mu(settings, tmp_path))
    user = reg.tenancy.create_account("newcomer", "n@example.com", "google", "google:n@example.com")
    assert user.handle == "newcomer"
    assert reg.tenancy.user_by_subject("google:n@example.com").id == user.id


def test_signup_still_enforces_handle_rules(settings, tmp_path):
    reg = StoreRegistry(_mu(settings, tmp_path))
    for bad in ("hiren", "admin", "con", "a/b", ""):
        with pytest.raises(TenancyError):
            reg.tenancy.create_account(bad, f"{bad or 'x'}@example.com", "google", f"google:{bad}")


def _client(settings, tmp_path, open_signup=True):
    mu = _mu(settings, tmp_path, open_signup=open_signup)
    mcp = FastMCP("d")
    dash = register_dashboard(mcp, mu, StoreRegistry(mu), {"google": FakeIdP()})
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app, base_url="https://testserver"), dash


def test_join_without_token_offers_signup(settings, tmp_path):
    client, _ = _client(settings, tmp_path, open_signup=True)
    r = client.get("/join", follow_redirects=False)
    assert r.status_code == 200
    assert "Create your Engram" in r.text
    assert "Sign in with Google" in r.text


def test_join_without_token_is_refused_when_signups_closed(settings, tmp_path):
    client, _ = _client(settings, tmp_path, open_signup=False)
    r = client.get("/join", follow_redirects=False)
    assert r.status_code == 403
    assert "isn't accepting new accounts" in r.text


def test_signup_oauth_start_blocked_when_closed(settings, tmp_path):
    client, _ = _client(settings, tmp_path, open_signup=False)
    assert client.get("/dashboard/auth/google?kind=signup", follow_redirects=False).status_code == 403


def test_full_signup_flow_creates_account_and_brain(settings, tmp_path):
    client, dash = _client(settings, tmp_path, open_signup=True)
    # start signup -> oauth -> callback lands on the handle-claim form
    start = client.get("/dashboard/auth/google?kind=signup", follow_redirects=False)
    state = start.headers["location"].split("state=")[1]
    cb = client.get(f"/oauth/callback/dashboard?state={state}&code=abc", follow_redirects=False)
    assert cb.status_code == 200 and "Choose your handle" in cb.text
    # claim it
    done = client.post("/join/claim", data={"handle": "newcomer"}, follow_redirects=False)
    assert done.status_code == 302
    user = dash.registry.tenancy.user_by_handle("newcomer")
    assert user is not None
    assert (dash.settings.users_root and
            (__import__("pathlib").Path(dash.settings.users_root) / "newcomer" / "brain" / "index.md").is_file())


# -- project organization ---------------------------------------------------


@pytest.fixture()
async def store(settings):
    s = KBStore(settings)
    await s.start()
    return s


def _proj(desc):
    return f"---\ntype: project\ndescription: {desc}\n---\n\n# About\n\n{desc}\n"


@pytest.mark.asyncio
async def test_tag_and_archive_projects(store):
    await store.kb_write("projects/tagged/context.md", _proj("a project"), "seed")
    res = await store.kb_tag_project("tagged", tags=["Client-Work", "alt"])
    assert res["tags"] == ["client-work", "alt"]  # normalized to lowercase

    listing = {p["id"]: p for p in await store.kb_projects()}
    assert listing["tagged"]["tags"] == ["client-work", "alt"]

    await store.kb_tag_project("tagged", status="archived")
    assert (await store.kb_projects())[0]["status"] in ("archived", "active")
    assert {p["id"]: p for p in await store.kb_projects()}["tagged"]["status"] == "archived"

    await store.kb_tag_project("tagged", tags=[])  # clearing works
    assert {p["id"]: p for p in await store.kb_projects()}["tagged"]["tags"] == []


@pytest.mark.asyncio
async def test_projects_report_visibility(store):
    await store.kb_write("projects/openp/context.md", _proj("open"), "seed")
    await store.kb_write("projects/shutp/context.md", _proj("shut"), "seed")
    await store.kb_publish("projects/openp/context.md", "public")
    listing = {p["id"]: p for p in await store.kb_projects()}
    assert listing["openp"]["visibility"] == "public"
    assert listing["shutp"]["visibility"] == "private"


@pytest.mark.asyncio
async def test_ensure_project_is_idempotent(store):
    first = await store.ensure_project("brandnew", "a fresh project")
    assert first["created"] is True
    again = await store.ensure_project("brandnew", "ignored")
    assert again["created"] is False
    assert "brandnew" in [p["id"] for p in await store.kb_projects()]


# -- visibility marking -----------------------------------------------------


@pytest.mark.parametrize(
    "vis,expected",
    [("public", "🌐 public"), ("contacts", "👥 contacts"), ("private", "🔒 private")],
)
def test_every_visibility_state_is_marked(vis, expected):
    """Exposure must never be inferred from the ABSENCE of a badge."""
    badge = Dashboard._vis_badge(vis)
    assert expected in badge
    assert f"vis-{vis}" in badge


def test_project_card_shows_visibility_and_tags():
    card = Dashboard._project_card(
        {"id": "alt", "title": "Alt", "description": "d", "visibility": "public",
         "tags": ["client-work"], "last_session": "2026-07-30"}
    )
    assert "🌐 public" in card and "client-work" in card and "/dashboard/p/alt" in card
