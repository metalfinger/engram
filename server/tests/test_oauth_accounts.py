"""M1.1 — ProxyOAuthProvider with an injected account-based allow_subject predicate."""

from __future__ import annotations

import pytest
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from engram_server.oauth.idp import UpstreamUser
from engram_server.oauth.provider import LoginNotAllowedError, ProxyOAuthProvider, handle_callback
from engram_server.oauth.store import InMemoryOAuthStore, PendingAuth

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


class FakeIdP:
    name = "google"

    def __init__(self, login: str):
        self.user = UpstreamUser(id="sub-1", login=login)

    def authorize_url(self, redirect_uri, state):
        return f"https://accounts.example/authorize?state={state}"

    async def exchange_code(self, code, redirect_uri, http):
        return "upstream-token"

    async def fetch_user(self, upstream_token, http):
        return self.user


def make_provider(login, allow_subject):
    return ProxyOAuthProvider(
        store=InMemoryOAuthStore(),
        idp=FakeIdP(login),
        public_url="https://engram.metalfinger.xyz",
        callback_path="/oauth/callback",
        allow_subject=allow_subject,
    )


def seed_pending(provider, nonce="nonce-1"):
    provider.store.put_pending(
        nonce,
        PendingAuth(
            client_id="client-1", redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True, code_challenge="c",
            scopes=["mcp"], client_state="cs", resource=None,
        ),
    )
    return nonce


def make_client():
    return OAuthClientInformationFull(client_id="client-1", redirect_uris=[AnyUrl(REDIRECT_URI)])


def test_allow_subject_predicate_gates_by_exact_subject():
    active = {"google:a@example.com", "github:metalfinger"}
    provider = make_provider("a@example.com", allow_subject=lambda s: s in active)
    assert provider.is_allowed(UpstreamUser(id="1", login="a@example.com"))
    assert not provider.is_allowed(UpstreamUser(id="2", login="stranger@example.com"))


@pytest.mark.asyncio
async def test_callback_admits_active_account():
    provider = make_provider("a@example.com", allow_subject=lambda s: s == "google:a@example.com")
    nonce = seed_pending(provider)
    redirect = await handle_callback(provider, "code", nonce, None)
    assert "code=" in redirect


@pytest.mark.asyncio
async def test_callback_rejects_unknown_account():
    provider = make_provider("stranger@example.com", allow_subject=lambda s: False)
    nonce = seed_pending(provider)
    with pytest.raises(LoginNotAllowedError):
        await handle_callback(provider, "code", nonce, None)


@pytest.mark.asyncio
async def test_refresh_denies_subject_that_went_inactive():
    """A suspended/removed account cannot keep refreshing (predicate now returns False)."""
    allowed = {"google:a@example.com"}
    provider = make_provider("a@example.com", allow_subject=lambda s: s in allowed)
    rt = RefreshToken(token="rt", client_id="client-1", scopes=["mcp"], subject="google:a@example.com")
    provider.store.put_refresh(rt)
    # first refresh works
    await provider.exchange_refresh_token(make_client(), rt, ["mcp"])
    # account suspended -> predicate flips -> refresh denied and token revoked
    allowed.clear()
    with pytest.raises(TokenError) as exc:
        await provider.exchange_refresh_token(make_client(), rt, ["mcp"])
    assert exc.value.error == "invalid_grant"
    assert provider.store.get_refresh("rt") is None


def test_app_allow_subject_owner_and_accounts(settings, tmp_path, monkeypatch):
    """The real _allow_subject wiring in app.py: owner always, accounts in multiuser."""
    import engram_server.app as app_module
    from engram_server.registry import StoreRegistry

    mu = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
    })
    registry = StoreRegistry(mu)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", mu)

    # owner always allowed
    assert app_module._allow_subject("github:metalfinger")
    # unknown refused
    assert not app_module._allow_subject("google:stranger@example.com")
    # active account allowed; suspended refused
    inv = registry.tenancy.create_invite("a@example.com")
    registry.tenancy.accept_invite(inv.token, "amiya", "a@example.com", "google", "google:a@example.com")
    assert app_module._allow_subject("google:a@example.com")
    registry.tenancy.set_status("amiya", "suspended")
    assert not app_module._allow_subject("google:a@example.com")


def test_app_allow_subject_single_user_is_owner_only(settings, monkeypatch):
    import engram_server.app as app_module

    monkeypatch.setattr(app_module, "settings", settings.model_copy(update={"multiuser": False}))
    assert app_module._allow_subject("github:metalfinger")
    assert not app_module._allow_subject("google:anyone@example.com")
