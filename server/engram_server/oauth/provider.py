from __future__ import annotations

import secrets
import time
from typing import Callable

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from engram_server.oauth.idp import UpstreamIdP, UpstreamUser
from engram_server.oauth.store import InMemoryOAuthStore, PendingAuth

CODE_TTL = 600
ACCESS_TTL = 3600


class LoginNotAllowedError(Exception):
    """Upstream IdP authenticated a user who is not on the allowlist.

    Raised by handle_callback; the callback route catches it (before any generic
    handler) and renders a 403 page.
    """

    def __init__(self, login: str):
        super().__init__(f"login not allowed: {login}")
        self.login = login


class ProxyOAuthProvider:
    def __init__(
        self,
        store: InMemoryOAuthStore,
        idp: UpstreamIdP,
        public_url: str,
        callback_path: str,
        allowed_logins: frozenset[str] = frozenset(),
        allow_subject: Callable[[str], bool] | None = None,
        token_factory: Callable[..., str] = secrets.token_urlsafe,
    ):
        self.store = store
        self.idp = idp
        self.public_url = public_url.rstrip("/")
        self.callback_path = callback_path
        self._new = token_factory
        # M1: authorization is a subject-level predicate. In multiuser it consults
        # the tenancy DB (owner + active accounts); pass ``allow_subject``. When it's
        # absent we fall back to the legacy static allowlist (single-user, tests) so
        # existing callers keep working unchanged.
        legacy = frozenset(login.lower() for login in allowed_logins)
        self.allowed_logins = legacy
        self._allow_subject = allow_subject or self._legacy_allow

    def _legacy_allow(self, subject: str) -> bool:
        """Static-allowlist predicate (single-user / tests): '<idp>:<login>' in the set."""
        login = self._login_from_subject(subject)
        return login is not None and login.lower() in self.allowed_logins

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}{self.callback_path}"

    # --- authorization ---
    def allow_subject(self, subject: str) -> bool:
        """Is this token subject permitted to connect? Fails closed."""
        return self._allow_subject(subject)

    def is_allowed(self, user: UpstreamUser) -> bool:
        """Whether an upstream-authenticated user may mint a token."""
        return self._allow_subject(self.subject_for(user))

    def subject_for(self, user: UpstreamUser) -> str:
        """Stable token subject, e.g. 'github:metalfinger'."""
        return f"{self.idp.name}:{user.login.lower()}"

    def _login_from_subject(self, subject: str | None) -> str | None:
        """Reverse subject_for: strip this IdP's prefix; None if it doesn't match."""
        prefix = f"{self.idp.name}:"
        if subject is None or not subject.startswith(prefix):
            return None
        return subject[len(prefix) :]

    # --- client registration (DCR) ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info)

    # --- authorize: redirect user to the upstream IdP ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        nonce = secrets.token_urlsafe(24)
        self.store.put_pending(
            nonce,
            PendingAuth(
                client_id=client.client_id,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                scopes=params.scopes or [],
                client_state=params.state,
                resource=params.resource,
            ),
        )
        return self.idp.authorize_url(self.callback_url, nonce)

    # --- authorization codes ---
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.store.get_code(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.store.del_code(authorization_code.code)
        access = self._new(32)
        refresh = self._new(32)
        self.store.put_access(
            AccessToken(
                token=access,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                expires_at=int(time.time()) + ACCESS_TTL,
                subject=authorization_code.subject,
            )
        )
        self.store.put_refresh(
            RefreshToken(
                token=refresh,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                subject=authorization_code.subject,
            )
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes),
        )

    # --- refresh tokens ---
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rt = self.store.get_refresh(refresh_token)
        if rt is None or rt.client_id != client.client_id:
            return None
        return rt

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Re-check authorization: an account suspended/removed after issuance must not
        # keep refreshing. The SDK's TokenHandler catches TokenError -> RFC 6749 (HTTP 400).
        subject = refresh_token.subject
        if subject is None or not self._allow_subject(subject):
            self.store.del_refresh(refresh_token.token)
            raise TokenError(error="invalid_grant", error_description="account is no longer allowed")
        access = self._new(32)
        self.store.put_access(
            AccessToken(
                token=access,
                client_id=client.client_id,
                scopes=scopes or refresh_token.scopes,
                expires_at=int(time.time()) + ACCESS_TTL,
                subject=subject,
            )
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh_token.token,
            scope=" ".join(scopes or refresh_token.scopes),
        )

    # --- access tokens ---
    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self.store.get_access(token)
        if at is None:
            return None
        if at.expires_at is not None and at.expires_at < int(time.time()):
            self.store.del_access(token)
            return None
        return at

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.del_access(token.token)
        self.store.del_refresh(token.token)


async def handle_callback(
    provider: ProxyOAuthProvider, code: str, state: str, http: httpx.AsyncClient
) -> str:
    pending = provider.store.pop_pending(state)
    if pending is None:
        raise ValueError("unknown or expired state")
    upstream_token = await provider.idp.exchange_code(code, provider.callback_url, http)
    user = await provider.idp.fetch_user(upstream_token, http)
    if not provider.is_allowed(user):
        raise LoginNotAllowedError(user.login)

    our_code = provider._new(32)
    provider.store.put_code(
        AuthorizationCode(
            code=our_code,
            scopes=pending.scopes,
            expires_at=time.time() + CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.code_challenge,
            redirect_uri=AnyUrl(pending.redirect_uri),
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            subject=provider.subject_for(user),
        )
    )
    params = {"code": our_code}
    if pending.client_state is not None:
        params["state"] = pending.client_state
    return construct_redirect_uri(str(pending.redirect_uri), **params)
