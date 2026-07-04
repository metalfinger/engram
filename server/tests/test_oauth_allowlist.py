"""Allowlist enforcement in ProxyOAuthProvider — unit tests, no network, no conftest fixtures."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from engram_server.oauth.idp import UpstreamUser
from engram_server.oauth.provider import LoginNotAllowedError, ProxyOAuthProvider, handle_callback
from engram_server.oauth.store import InMemoryOAuthStore, PendingAuth

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


class FakeIdP:
    """Stub upstream IdP: no HTTP, fixed user."""

    name = "github"

    def __init__(self, login: str, user_id: str = "42"):
        self.user = UpstreamUser(id=user_id, login=login)

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return f"https://github.example/authorize?state={state}"

    async def exchange_code(self, code: str, redirect_uri: str, http) -> str:
        return "upstream-token"

    async def fetch_user(self, upstream_token: str, http) -> UpstreamUser:
        return self.user


def make_provider(
    login: str = "MetalFinger",
    allowed: tuple[str, ...] = ("metalfinger",),
    store: InMemoryOAuthStore | None = None,
) -> ProxyOAuthProvider:
    return ProxyOAuthProvider(
        store=store or InMemoryOAuthStore(),
        idp=FakeIdP(login),
        public_url="https://engram.metalfinger.xyz",
        callback_path="/oauth/callback",
        allowed_logins=frozenset(allowed),
    )


def seed_pending(provider: ProxyOAuthProvider, nonce: str = "nonce-1") -> str:
    provider.store.put_pending(
        nonce,
        PendingAuth(
            client_id="client-1",
            redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            code_challenge="challenge",
            scopes=["user"],
            client_state="client-state",
            resource=None,
        ),
    )
    return nonce


def make_client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=[AnyUrl(REDIRECT_URI)])


# --- allowlist semantics ---


def test_allowlist_is_case_insensitive():
    provider = make_provider(allowed=("MetalFinger",))
    assert provider.is_allowed(UpstreamUser(id="1", login="metalfinger"))
    assert provider.is_allowed(UpstreamUser(id="1", login="METALFINGER"))
    provider = make_provider(allowed=("metalfinger",))
    assert provider.is_allowed(UpstreamUser(id="1", login="MetalFinger"))


def test_empty_allowlist_fails_closed():
    provider = make_provider(allowed=())
    assert not provider.is_allowed(UpstreamUser(id="1", login="metalfinger"))
    assert not provider.is_allowed(UpstreamUser(id="2", login=""))


def test_subject_is_idp_prefixed_lowercase_login():
    provider = make_provider()
    assert provider.subject_for(UpstreamUser(id="1", login="MetalFinger")) == "github:metalfinger"


# --- handle_callback ---


async def test_handle_callback_rejects_stranger():
    provider = make_provider(login="stranger")
    nonce = seed_pending(provider)
    with pytest.raises(LoginNotAllowedError) as exc_info:
        await handle_callback(provider, "gh-code", nonce, None)
    assert exc_info.value.login == "stranger"


async def test_handle_callback_mints_code_for_owner():
    provider = make_provider(login="MetalFinger")
    nonce = seed_pending(provider)
    redirect = await handle_callback(provider, "gh-code", nonce, None)
    query = parse_qs(urlparse(redirect).query)
    assert query["state"] == ["client-state"]
    code = provider.store.get_code(query["code"][0])
    assert code is not None
    assert code.subject == "github:metalfinger"
    assert code.client_id == "client-1"


# --- refresh-token re-check ---


async def test_refresh_denies_removed_login():
    provider = make_provider(allowed=("metalfinger",))
    rt = RefreshToken(token="rt-ghost", client_id="client-1", scopes=["user"], subject="github:ghost")
    provider.store.put_refresh(rt)
    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_refresh_token(make_client(), rt, ["user"])
    assert exc_info.value.error == "invalid_grant"
    # the refresh token is revoked so a delisted login cannot retry
    assert provider.store.get_refresh("rt-ghost") is None


async def test_refresh_allows_current_login():
    provider = make_provider(allowed=("metalfinger",))
    rt = RefreshToken(token="rt-owner", client_id="client-1", scopes=["user"], subject="github:metalfinger")
    provider.store.put_refresh(rt)
    token = await provider.exchange_refresh_token(make_client(), rt, ["user"])
    access = provider.store.get_access(token.access_token)
    assert access is not None
    assert access.subject == "github:metalfinger"


# --- store persistence ---


def test_store_roundtrips_clients_and_access_tokens(tmp_path):
    path = str(tmp_path / "oauth_store.json")
    store = InMemoryOAuthStore(path)
    store.put_client(make_client("client-persist"))
    store.put_access(
        AccessToken(
            token="at-1",
            client_id="client-persist",
            scopes=["user"],
            expires_at=2_000_000_000,
            subject="github:metalfinger",
        )
    )

    reloaded = InMemoryOAuthStore(path)
    client = reloaded.get_client("client-persist")
    assert client is not None
    assert client.redirect_uris == [AnyUrl(REDIRECT_URI)]
    access = reloaded.get_access("at-1")
    assert access is not None
    assert access.subject == "github:metalfinger"
    assert access.expires_at == 2_000_000_000
