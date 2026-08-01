"""One-door auth for the legacy explorer routes (v3 Wave 1).

The explorer used to sit behind Cloudflare Access — a SECOND identity system
(an email allowlist in Zero Trust) completely separate from Engram accounts.
That was a v1 shortcut from before accounts existed; M1 built the real thing
(browser OAuth -> signed session cookie), so the guard now verifies the SAME
``engram_session`` cookie the dashboard issues. One account, one door.

/brain/* renders the OWNER's whole brain, so these routes are owner-only:
teammates who sign in are redirected to /dashboard — their own per-user home.

SECURITY: cloudflared runs on this same machine, so EVERY tunneled request —
including a hostile one that reached the raw port — arrives from 127.0.0.1.
An IP-based ("localhost is trusted") bypass would therefore open the explorer
to the whole internet. Never add one. The only accepted proofs are a valid
owner session cookie, the legacy Cf-Access-Jwt-Assertion JWT (kept as a
fallback for single-user deployments with no dashboard secret), or the
explicit dev_no_access setting (local development only).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import jwt
from anyio import to_thread
from jwt import PyJWKClient
from starlette.responses import PlainTextResponse, RedirectResponse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from engram_server.config import Settings

Handler = Callable[["Request"], Awaitable["Response"]]


class CfAccessVerifier:
    """Verifies Cloudflare Access JWTs against the team's JWKS endpoint.

    LEGACY: only used when no dashboard session secret is configured (a
    single-user deployment that never enabled the account system). The live
    multi-user server authenticates with Engram's own session cookie instead.
    """

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
    """Build a decorator wrapping explorer handlers with owner auth.

    On success the verified identity lands in ``request.state.viewer``.
    Resolution order:
    1. dev_no_access — local development bypass, never in production.
    2. dashboard session cookie (the one door): owner -> allowed; a signed-in
       teammate -> 302 to /dashboard (their own surface); nobody -> 302 to the
       login page. Browsers get redirects, not 403 walls.
    3. No dashboard secret configured -> the legacy Cloudflare Access JWT path.
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

    if settings.dashboard_session_secret:
        # Lazy import: dashboard.py imports explorer helpers at module level, so a
        # module-level import here would risk a cycle.
        from engram_server.dashboard import SESSION_COOKIE, DashboardAuth

        auth = DashboardAuth(
            settings.dashboard_session_secret,
            settings.dashboard_session_ttl_hours * 3600,
        )
        owner_subjects = frozenset(
            s.strip() for s in settings.owner_subjects.split(",") if s.strip()
        )

        def guarded_session(handler: Handler) -> Handler:
            @functools.wraps(handler)
            async def wrapped(request: Request) -> Response:
                session = auth.verify(request.cookies.get(SESSION_COOKIE))
                if session is None:
                    return RedirectResponse("/dashboard/login", status_code=302)
                if session.get("sub") not in owner_subjects:
                    # A real teammate — but /brain/* is the OWNER's brain. Their
                    # per-user home is the dashboard.
                    return RedirectResponse("/dashboard", status_code=302)
                request.state.viewer = session.get("email") or session.get("sub") or "owner"
                return await handler(request)

            return wrapped

        return guarded_session

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
