"""Dashboard-minted API keys — for apps that take a bearer token but can't run OAuth.

A key acts as its owner with full access, so every test here is about the ways that
could go wrong: the raw key stored or shown twice, one account touching another's
keys, a revoked key or a suspended account still getting in.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.dashboard import register_dashboard
from engram_server.kbstore import _scan_secrets
from engram_server.oauth.provider import ProxyOAuthProvider
from engram_server.oauth.store import InMemoryOAuthStore
from engram_server.registry import StoreRegistry
from engram_server.tenancy import TenancyError, TenancyStore


@pytest.fixture()
def tenancy(tmp_path) -> TenancyStore:
    t = TenancyStore(tmp_path / "engram.db")
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        t.create_account(h, e, "github", f"github:{h}")
    return t


def _uid(t: TenancyStore, handle: str) -> int:
    return t.user_by_handle(handle).id  # type: ignore[union-attr]


# -- store -------------------------------------------------------------------


def test_minted_key_resolves_to_its_owner(tenancy):
    key, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    assert raw.startswith("egk_") and len(raw) > 40
    assert key.prefix == raw[:12]
    assert tenancy.subject_for_api_key(raw) == "github:alice"


def test_only_the_hash_is_stored(tenancy, tmp_path):
    _, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    dump = "\n".join(sqlite3.connect(tmp_path / "engram.db").iterdump())
    assert raw not in dump, "the raw key must never reach the database"


def test_wrong_or_foreign_tokens_resolve_to_nobody(tenancy):
    tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    assert tenancy.subject_for_api_key("egk_" + "x" * 43) is None
    assert tenancy.subject_for_api_key("some-oauth-access-token") is None
    assert tenancy.subject_for_api_key("") is None


def test_a_revoked_key_stops_resolving(tenancy):
    key, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    tenancy.revoke_api_key(_uid(tenancy, "alice"), key.id)
    assert tenancy.subject_for_api_key(raw) is None
    assert tenancy.list_api_keys(_uid(tenancy, "alice")) == []


def test_nobody_can_revoke_another_accounts_key(tenancy):
    key, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    with pytest.raises(TenancyError):
        tenancy.revoke_api_key(_uid(tenancy, "bob"), key.id)
    assert tenancy.subject_for_api_key(raw) == "github:alice", "bob's attempt must change nothing"


def test_keys_are_listed_per_account(tenancy):
    tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    assert [k.name for k in tenancy.list_api_keys(_uid(tenancy, "alice"))] == ["hark"]
    assert tenancy.list_api_keys(_uid(tenancy, "bob")) == []


def test_a_key_needs_a_name(tenancy):
    with pytest.raises(TenancyError, match="name"):
        tenancy.create_api_key(_uid(tenancy, "alice"), "   ")


def test_live_keys_are_capped(tenancy):
    uid = _uid(tenancy, "alice")
    for i in range(20):
        tenancy.create_api_key(uid, f"k{i}")
    with pytest.raises(TenancyError, match="revoke"):
        tenancy.create_api_key(uid, "one too many")


def test_use_is_recorded(tenancy):
    key, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    assert key.last_used is None
    tenancy.subject_for_api_key(raw)
    assert tenancy.list_api_keys(_uid(tenancy, "alice"))[0].last_used is not None


# -- auth layer --------------------------------------------------------------


class _IdP:
    name = "github"


def _provider(tenancy, allowed=lambda s: True) -> ProxyOAuthProvider:
    return ProxyOAuthProvider(
        store=InMemoryOAuthStore(),
        idp=_IdP(),  # type: ignore[arg-type]
        public_url="https://engram.example",
        callback_path="/oauth/callback",
        allow_subject=allowed,
        api_key_subject=tenancy.subject_for_api_key,
    )


@pytest.mark.asyncio
async def test_an_api_key_authenticates_as_its_owner(tenancy):
    _, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    at = await _provider(tenancy).load_access_token(raw)
    assert at is not None
    assert at.subject == "github:alice"
    assert at.scopes == ["mcp"], "the /mcp route requires the mcp scope"
    assert at.expires_at is None


@pytest.mark.asyncio
async def test_a_suspended_account_fails_closed_even_with_a_live_key(tenancy):
    """Suspending the account must be enough on its own; nobody should have to
    remember to revoke its keys too."""
    _, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    tenancy.set_status("alice", "suspended")

    def allowed(subject: str) -> bool:
        u = tenancy.user_by_subject(subject)
        return u is not None and u.status == "active"

    assert await _provider(tenancy, allowed).load_access_token(raw) is None


@pytest.mark.asyncio
async def test_a_revoked_key_is_refused_by_the_auth_layer(tenancy):
    key, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    p = _provider(tenancy)
    assert await p.load_access_token(raw) is not None
    tenancy.revoke_api_key(_uid(tenancy, "alice"), key.id)
    assert await p.load_access_token(raw) is None, "revocation must bite on the next request"


@pytest.mark.asyncio
async def test_without_the_hook_api_keys_are_not_accepted(tenancy):
    _, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    p = ProxyOAuthProvider(
        store=InMemoryOAuthStore(), idp=_IdP(),  # type: ignore[arg-type]
        public_url="https://engram.example", callback_path="/oauth/callback",
        allow_subject=lambda s: True,
    )
    assert await p.load_access_token(raw) is None


def test_the_secret_scanner_catches_a_pasted_key(tenancy):
    _, raw = tenancy.create_api_key(_uid(tenancy, "alice"), "hark")
    assert any(kind == "Engram API key" for kind, _ in _scan_secrets(f"my key is {raw}"))


def test_the_secret_scanner_catches_a_money_key():
    # Money MCP mints mnk_ keys the same way (secrets.token_urlsafe(32)); one
    # pasted into the brain would be committed to git with full ledger access.
    import secrets

    raw = "mnk_" + secrets.token_urlsafe(32)
    assert any(kind == "Money API key" for kind, _ in _scan_secrets(f"money key: {raw}"))
    assert _scan_secrets("the mnk_ prefix names Money keys") == [], "the bare prefix is not a key"


# -- dashboard ---------------------------------------------------------------


class _FakeIdP:
    name = "google"

    def authorize_url(self, r, s): return "https://x/auth"
    async def exchange_code(self, c, r, h): return "t"
    async def fetch_user(self, t, h): return SimpleNamespace(id="x", login="a@example.com")


@pytest.fixture()
def web(settings, tmp_path):
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
    dash = register_dashboard(mcp, mu, registry, {"google": _FakeIdP()})
    client = TestClient(Starlette(routes=mcp._custom_starlette_routes), base_url="https://testserver")
    cookies = {
        h: {"engram_session": dash.auth.issue(f"google:{e}", e, h, ttl=3600)}
        for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com"))
    }
    return SimpleNamespace(client=client, registry=registry, users=users, cookies=cookies)


def test_keys_page_requires_login(web):
    assert web.client.get("/dashboard/keys", follow_redirects=False).status_code == 302


def test_creating_a_key_shows_it_once_and_never_again(web):
    r = web.client.post("/dashboard/keys/create", cookies=web.cookies["alice"], data={"name": "hark"})
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    keys = web.registry.tenancy.list_api_keys(web.users["alice"].id)
    assert len(keys) == 1
    raw = r.text.split("user-select:all;word-break:break-all'>", 1)[1].split("<", 1)[0]
    assert raw.startswith("egk_")
    assert web.registry.tenancy.subject_for_api_key(raw) == "google:alice@example.com"

    again = web.client.get("/dashboard/keys", cookies=web.cookies["alice"])
    assert "hark" in again.text
    assert raw not in again.text, "the list must show only the prefix, never the full key"


def test_revoking_from_the_dashboard(web):
    key, raw = web.registry.tenancy.create_api_key(web.users["alice"].id, "hark")
    r = web.client.post("/dashboard/keys/revoke", cookies=web.cookies["alice"], data={"id": str(key.id)})
    assert r.status_code == 200
    assert web.registry.tenancy.subject_for_api_key(raw) is None


def test_the_dashboard_will_not_revoke_someone_elses_key(web):
    key, raw = web.registry.tenancy.create_api_key(web.users["alice"].id, "hark")
    r = web.client.post("/dashboard/keys/revoke", cookies=web.cookies["bob"], data={"id": str(key.id)})
    assert r.status_code == 400
    assert web.registry.tenancy.subject_for_api_key(raw) == "google:alice@example.com"
