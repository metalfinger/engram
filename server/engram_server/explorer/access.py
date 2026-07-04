"""Cloudflare Access JWT verification for the explorer routes.

SECURITY: cloudflared runs on this same machine, so EVERY tunneled request —
including a hostile one that reached the raw port — arrives from 127.0.0.1.
An IP-based ("localhost is trusted") bypass would therefore open the explorer
to the whole internet. Never add one. The only accepted proofs are a valid
Cf-Access-Jwt-Assertion JWT or the explicit dev_no_access setting (local
development only).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import jwt
from anyio import to_thread
from jwt import PyJWKClient
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from engram_server.config import Settings

Handler = Callable[["Request"], Awaitable["Response"]]


class CfAccessVerifier:
    """Verifies Cloudflare Access JWTs against the team's JWKS endpoint."""

    def __init__(self, team_domain: str, aud: str) -> None:
        self.issuer = f"https://{team_domain}"
        self.aud = aud
        self._jwks = PyJWKClient(
            f"https://{team_domain}/cdn-cgi/access/certs",
            cache_keys=True,
            lifespan=86400,
        )

    def verify(self, token: str) -> str | None:
        """Return the authenticated identity (email claim, else sub) or None.

        SYNC — may hit the network on a cold JWKS cache; callers run it in a
        worker thread (anyio.to_thread).
        """
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.aud,
                issuer=self.issuer,
            )
        except jwt.PyJWTError:
            return None
        return claims.get("email") or claims.get("sub")


def make_guard(settings: Settings) -> Callable[[Handler], Handler]:
    """Build a decorator wrapping explorer handlers with Cloudflare Access auth.

    On success the verified identity lands in ``request.state.viewer``.
    """
    if settings.dev_no_access:
        # Local development only — must never be enabled in production.
        def guarded_dev(handler: Handler) -> Handler:
            @functools.wraps(handler)
            async def wrapped(request: Request) -> Response:
                request.state.viewer = "dev"
                return await handler(request)

            return wrapped

        return guarded_dev

    verifier = CfAccessVerifier(settings.cf_access_team_domain, settings.cf_access_aud)

    def guarded(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapped(request: Request) -> Response:
            token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get(
                "CF_Authorization"
            )
            viewer: str | None = None
            if token:
                viewer = await to_thread.run_sync(verifier.verify, token)
            if viewer is None:
                return PlainTextResponse(
                    "403 Forbidden: this page sits behind Cloudflare Access. "
                    f"Open it via {settings.explorer_url} and sign in there.",
                    status_code=403,
                )
            request.state.viewer = viewer
            return await handler(request)

        return wrapped

    return guarded
