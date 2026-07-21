"""Dashboard + onboarding (v2 M1.4) — the browser front-of-house for multi-user Engram.

Three surfaces, all on the MCP host, all with their OWN signed-cookie session
(separate from the MCP bearer-token OAuth and from the explorer's Cloudflare Access):

  /join?token=…   — an invitee clicks the emailed magic link, signs in with
                    GitHub/Google, claims a @handle; we create their account
                    (tenancy.accept_invite) and provision their brain. The invite
                    TOKEN is the email-possession proof, so the account can bind
                    whatever idp_subject they sign in with (a GitHub login need not
                    equal the invited email).
  /dashboard      — after sign-in: your handle + how to connect your AI. The owner
                    additionally gets the admin panel (members + invites).
  /dashboard/…    — sign-in (GitHub/Google browser OAuth -> signed session cookie),
                    invite create/revoke (owner only), logout.

Security properties enforced here:
  * Session cookie is a PyJWT HS256 token signed with dashboard_session_secret;
    tamper/expiry -> no session. HttpOnly + Secure + SameSite=Lax.
  * Invite create/revoke are owner-only (session subject in owner_subjects).
  * accept_invite + ensure_user_brain run inside tenancy's own integrity checks
    (unique handle/email/subject, live-invite, reserved/device-name handles).

The route handlers are thin adapters over pure-ish methods (issue/verify session,
_claim, _create_invite, _render_*) so the security logic is unit-tested without
constructing Starlette requests.
"""

from __future__ import annotations

import html as _html
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import httpx
import jwt
from anyio import to_thread
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from engram_server.errors import KBError
from engram_server.provisioning import ensure_user_brain
from engram_server.social import SocialError
from engram_server.tenancy import TenancyError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from engram_server.config import Settings
    from engram_server.oauth.idp import UpstreamIdP
    from engram_server.registry import StoreRegistry

SESSION_COOKIE = "engram_session"
OAUTH_STATE_COOKIE = "engram_oauth_state"
# Nested UNDER the MCP connector's /oauth/callback so a GitHub OAuth App that already
# registered /oauth/callback covers this too (GitHub allows any redirect that is a
# subdirectory of the registered callback) — the operator needs no GitHub change.
# (Google requires exact-match, so a Google app registers this full path.)
CALLBACK_PATH = "/oauth/callback/dashboard"


class DashboardAuth:
    """Signed-cookie session: a PyJWT HS256 token carrying the signed-in identity."""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret
        self._ttl = ttl_seconds

    def issue(self, subject: str, email: str, handle: str | None,
              ttl: int | None = None, scope: str = "session") -> str:
        now = int(time.time())
        return jwt.encode(
            {"sub": subject, "email": email, "handle": handle, "scope": scope,
             "iat": now, "exp": now + (ttl if ttl is not None else self._ttl)},
            self._secret,
            algorithm="HS256",
        )

    def verify(self, cookie: str | None, expected_scope: str = "session") -> dict | None:
        """Decode + validate a token, REQUIRING it was issued for ``expected_scope``.
        This stops a token minted for one purpose (e.g. the long-lived extension
        'notify' token) being replayed for another (a full dashboard 'session')."""
        if not cookie:
            return None
        try:
            claims = jwt.decode(cookie, self._secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        # Legacy/unscoped tokens count as 'session' so pre-scope cookies still verify.
        if claims.get("scope", "session") != expected_scope:
            return None
        return claims


@dataclass
class _Pending:
    idp_name: str
    kind: str  # "login" | "join" | "ext"
    invite_token: str | None
    ext_redirect: str | None = None


class Dashboard:
    def __init__(
        self,
        settings: "Settings",
        registry: "StoreRegistry",
        idps: dict[str, "UpstreamIdP"],
        *,
        auth: DashboardAuth | None = None,
        mailer: Callable | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.idps = idps
        self.auth = auth or DashboardAuth(
            settings.dashboard_session_secret,
            settings.dashboard_session_ttl_hours * 3600,
        )
        if mailer is not None:
            self._mailer = mailer
        else:  # deferred so dashboard.py imports without the mailer module present
            from engram_server.mailer import send_invite

            self._mailer = send_invite
        self._owner_subjects = frozenset(
            s.strip() for s in settings.owner_subjects.split(",") if s.strip()
        )
        self._pending: dict[str, _Pending] = {}

    # -- identity helpers ----------------------------------------------------

    @property
    def callback_url(self) -> str:
        return f"{self.settings.public_url}{CALLBACK_PATH}"

    def _is_owner(self, session: dict) -> bool:
        return session.get("sub") in self._owner_subjects

    def _subject_for(self, idp: "UpstreamIdP", login: str) -> str:
        return f"{idp.name}:{login.lower()}"

    def _account_handle(self, session: dict) -> str | None:
        """Current handle for a session subject (owner or a provisioned tenant)."""
        if self._is_owner(session):
            return self.settings.owner_handle
        user = self.registry.tenancy.user_by_subject(session.get("sub", ""))
        return user.handle if user else None

    # -- core operations (unit-tested directly) ------------------------------

    async def _claim(self, invite_token: str, handle: str, idp_name: str, subject: str) -> str:
        """Redeem an invite into an account + provisioned brain. Returns the handle.

        Raises TenancyError on any integrity failure (bad/used/expired invite,
        taken/reserved/device-name handle, duplicate email/subject) — the account
        row and the brain are only created together on success.
        """
        invite = self.registry.tenancy.get_invite(invite_token)
        if invite is None or not invite.live:
            raise TenancyError("This invite is no longer valid — ask for a fresh one.")
        # email = the invited address (magic-link possession proof), NOT the IdP email.
        user = self.registry.tenancy.accept_invite(
            invite_token, handle, invite.email, idp_name, subject
        )
        await to_thread.run_sync(lambda: ensure_user_brain(self.settings, user.handle))
        return user.handle

    async def _create_invite(self, email: str, inviter: dict) -> dict:
        """Owner creates an invite and (best-effort) emails the magic link."""
        user = self.registry.tenancy.user_by_subject(inviter.get("sub", ""))
        invited_by = user.id if user else None
        invite = self.registry.tenancy.create_invite(email, invited_by=invited_by)
        join_url = f"{self.settings.public_url}/join?token={invite.token}"
        sent = await self._mailer(
            self.settings,
            to_email=email,
            join_url=join_url,
            inviter_name=inviter.get("handle") or self.settings.owner_handle,
        )
        return {"invite": invite, "join_url": join_url, "mail": sent}

    # -- route handlers ------------------------------------------------------

    def _session(self, request: "Request") -> dict | None:
        return self.auth.verify(request.cookies.get(SESSION_COOKIE))

    async def dashboard(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        projects = await self._user_projects(session)
        return HTMLResponse(self._render_dashboard(session, projects=projects))

    # -- per-user brain browser (M4: the unified home browses YOUR own brain) ----

    async def _user_store(self, session: dict):
        """The signed-in user's OWN KBStore (owner -> the operator brain). None if no account."""
        handle = self._account_handle(session)
        if handle is None:
            return None
        try:
            return await self.registry.store_for_handle(handle)
        except KBError:
            return None

    async def _user_projects(self, session: dict) -> list:
        store = await self._user_store(session)
        if store is None:
            return []
        try:
            return await store.kb_projects()
        except Exception:  # noqa: BLE001 — a browse convenience, never fail the home
            return []

    async def browse_project(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        project = str(request.path_params.get("project", ""))
        if store is None:
            return HTMLResponse(self._page("No account", "<p>No brain to browse.</p>"), status_code=403)
        try:
            data = await store.kb_load(project)
        except KBError as exc:
            return HTMLResponse(self._page("Not found", f"<p>{_html.escape(str(exc))}</p>"
                                           "<p><a href='/dashboard'>&larr; home</a></p>"), status_code=404)
        return HTMLResponse(self._render_project(project, data))

    async def browse_concept(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        path = str(request.path_params.get("path", ""))
        if store is None:
            return HTMLResponse(self._page("No account", "<p>No brain to browse.</p>"), status_code=403)
        try:
            concept = await store.kb_read(path)  # path-safe + scoped to THIS user's brain
        except KBError as exc:
            return HTMLResponse(self._page("Not found", f"<p>{_html.escape(str(exc))}</p>"
                                           "<p><a href='/dashboard'>&larr; home</a></p>"), status_code=404)
        return HTMLResponse(self._render_concept(path, concept))

    async def login(self, request: "Request") -> "Response":
        return HTMLResponse(self._render_login(next_kind="login", invite_token=None))

    async def join(self, request: "Request") -> "Response":
        token = request.query_params.get("token", "")
        invite = self.registry.tenancy.get_invite(token)
        if invite is None or not invite.live:
            return HTMLResponse(
                self._page(
                    "Invite unavailable",
                    "<p>This invite link is invalid, already used, or expired. "
                    "Ask whoever invited you for a fresh one.</p>",
                ),
                status_code=400,
            )
        return HTMLResponse(self._render_login(next_kind="join", invite_token=token))

    @staticmethod
    def _valid_ext_redirect(url: str) -> bool:
        """A Chrome extension's launchWebAuthFlow redirect is always
        https://<extension-id>.chromiumapp.org/... — only allow that, so a
        signed-in user's notify token can never be exfiltrated to an arbitrary host."""
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        return parts.scheme == "https" and (parts.hostname or "").endswith(".chromiumapp.org")

    async def ext_auth(self, request: "Request") -> "Response":
        """Sign the Chrome notifier extension in with the SAME GitHub/Google OAuth
        as everything else, then hand it a scope='notify' token via the extension's
        chromiumapp.org redirect (no manual token paste)."""
        redirect = request.query_params.get("redirect", "")
        if not self._valid_ext_redirect(redirect):
            return PlainTextResponse("Invalid or missing extension redirect.", status_code=400)
        session = self._session(request)
        if session is not None:
            return self._ext_token_redirect(session["sub"], session.get("email", ""),
                                            self._account_handle(session), redirect)
        # No session yet — sign in first, carrying the ext redirect through OAuth.
        return HTMLResponse(self._render_login(next_kind="ext", invite_token=None, ext_redirect=redirect))

    def _ext_token_redirect(self, subject, email, handle, redirect) -> "Response":
        token = self.auth.issue(subject, email or "", handle,
                                ttl=self._EXT_TOKEN_TTL, scope="notify")
        return RedirectResponse(f"{redirect}#token={token}", status_code=302)

    async def start_oauth(self, request: "Request") -> "Response":
        idp_name = request.path_params.get("idp")
        idp = self.idps.get(idp_name or "")
        if idp is None:
            return PlainTextResponse(f"Unknown sign-in method: {idp_name}", status_code=404)
        kind = request.query_params.get("kind", "login")
        invite_token = request.query_params.get("token")
        ext_redirect = request.query_params.get("redirect")
        if kind == "join" and not invite_token:
            return PlainTextResponse("Missing invite token.", status_code=400)
        if kind == "ext" and not self._valid_ext_redirect(ext_redirect or ""):
            return PlainTextResponse("Invalid extension redirect.", status_code=400)
        state = secrets.token_urlsafe(24)
        self._pending[state] = _Pending(
            idp_name=idp.name, kind=kind, invite_token=invite_token, ext_redirect=ext_redirect
        )
        resp = RedirectResponse(idp.authorize_url(self.callback_url, state), status_code=302)
        # Bind this flow to THIS browser (OAuth login-CSRF / session-fixation defense):
        # the callback only proceeds if this cookie comes back matching the state param.
        resp.set_cookie(
            OAUTH_STATE_COOKIE, state,
            max_age=900, httponly=True, secure=True, samesite="lax", path="/",
        )
        return resp

    async def callback(self, request: "Request") -> "Response":
        # Delete the single-use state cookie on EVERY exit (pass or fail).
        resp = await self._callback(
            request.query_params.get("state", ""),
            request.query_params.get("code", ""),
            request.cookies.get(OAUTH_STATE_COOKIE),
        )
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return resp

    async def _callback(self, state: str, code: str, cookie_state: str | None) -> "Response":
        # The flow must complete in the SAME browser that started it: the state param
        # (from the IdP redirect) must equal the cookie we set in start_oauth. Without
        # this, an attacker who initiates a flow could fixate their code onto a victim.
        if not state or cookie_state is None or not secrets.compare_digest(cookie_state, state):
            return PlainTextResponse("Sign-in could not be verified — please start again.", status_code=400)
        pending = self._pending.pop(state, None)
        if pending is None:
            return PlainTextResponse("Sign-in session expired — please try again.", status_code=400)
        idp = self.idps.get(pending.idp_name)
        if idp is None or not code:
            return PlainTextResponse("Sign-in failed.", status_code=400)
        async with httpx.AsyncClient() as http:
            token = await idp.exchange_code(code, self.callback_url, http)
            user = await idp.fetch_user(token, http)
        subject = self._subject_for(idp, user.login)

        if pending.kind == "join":
            invite = self.registry.tenancy.get_invite(pending.invite_token or "")
            if invite is None or not invite.live:
                return HTMLResponse(
                    self._page("Invite unavailable", "<p>This invite is no longer valid. "
                               "Ask the person who invited you for a fresh link.</p>"),
                    status_code=400,
                )
            # Show the handle-claim form; the invite token + signed-in identity ride
            # in a short-lived cookie so the POST can complete without re-auth.
            resp = HTMLResponse(self._render_claim(user.login, invite.email))
            resp.set_cookie(
                "engram_onboarding",
                self.auth.issue(subject, invite.email, None, ttl=900, scope="onboarding"),
                max_age=900, httponly=True, secure=True, samesite="lax", path="/join",
            )
            resp.set_cookie(
                "engram_invite", pending.invite_token or "",
                max_age=900, httponly=True, secure=True, samesite="lax", path="/join",
            )
            return resp

        # login/ext: the subject must already be an account (or owner)
        handle = self._account_handle({"sub": subject})
        if handle is None:
            return HTMLResponse(
                self._page(
                    "No account yet",
                    "<p>That identity has no Engram account. If you have an invite, "
                    "open its link to accept it first.</p>",
                ),
                status_code=403,
            )
        if pending.kind == "ext":
            # Hand the extension its scope='notify' token via its chromiumapp.org redirect.
            return self._ext_token_redirect(subject, user.login, handle, pending.ext_redirect)
        return self._logged_in_redirect(subject, user.login, handle)

    async def claim(self, request: "Request") -> "Response":
        onboarding = self.auth.verify(request.cookies.get("engram_onboarding"), expected_scope="onboarding")
        invite_token = request.cookies.get("engram_invite")
        if onboarding is None or not invite_token:
            return PlainTextResponse("Onboarding session expired — reopen your invite link.", status_code=400)
        form = await request.form()
        handle = str(form.get("handle", "")).strip()
        idp_name = str(onboarding["sub"]).split(":", 1)[0]
        try:
            claimed = await self._claim(invite_token, handle, idp_name, onboarding["sub"])
        except TenancyError as exc:
            return HTMLResponse(self._render_claim(handle, onboarding["email"], error=str(exc)), status_code=400)
        resp = self._logged_in_redirect(onboarding["sub"], claimed, claimed)
        resp.delete_cookie("engram_onboarding", path="/join")
        resp.delete_cookie("engram_invite", path="/join")
        return resp

    async def create_invite(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None or not self._is_owner(session):
            return PlainTextResponse("Only the operator can send invites.", status_code=403)
        form = await request.form()
        email = str(form.get("email", "")).strip()
        try:
            result = await self._create_invite(email, session)
        except TenancyError as exc:
            return HTMLResponse(self._render_dashboard(session, error=str(exc)), status_code=400)
        note = (
            f"Invite emailed to {email}." if result["mail"].get("sent")
            else f"Invite created — email is off, copy this link: {result['join_url']}"
        )
        return HTMLResponse(self._render_dashboard(session, notice=note))

    async def create_github_invite(self, request: "Request") -> "Response":
        """Invite by GitHub username — bound to that GitHub identity, no email needed.
        The owner shares the returned link; only that GitHub account can accept."""
        session = self._session(request)
        if session is None or not self._is_owner(session):
            return PlainTextResponse("Only the operator can send invites.", status_code=403)
        form = await request.form()
        login = re.sub(r"[^a-zA-Z0-9-]", "", str(form.get("login", "")).strip().lstrip("@")).lower()
        if not login:
            return HTMLResponse(self._render_dashboard(session, error="Enter a GitHub username."), status_code=400)
        inviter = self.registry.tenancy.user_by_subject(session.get("sub", ""))
        try:
            invite = self.registry.tenancy.create_invite(
                f"{login}@users.noreply.github.com",
                invited_by=inviter.id if inviter else None,
                bind_subject=f"github:{login}",
            )
        except TenancyError as exc:
            return HTMLResponse(self._render_dashboard(session, error=str(exc)), status_code=400)
        join_url = f"{self.settings.public_url}/join?token={invite.token}"
        note = f"Invite for GitHub @{login} — send them this link: {join_url}"
        return HTMLResponse(self._render_dashboard(session, notice=note))

    async def revoke_invite(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None or not self._is_owner(session):
            return PlainTextResponse("Only the operator can revoke invites.", status_code=403)
        form = await request.form()
        try:
            self.registry.tenancy.revoke_invite(str(form.get("token", "")))
            return HTMLResponse(self._render_dashboard(session, notice="Invite revoked."))
        except TenancyError as exc:
            return HTMLResponse(self._render_dashboard(session, error=str(exc)), status_code=400)

    async def logout(self, request: "Request") -> "Response":
        resp = RedirectResponse("/dashboard/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    def _session_user(self, session: dict):
        return self.registry.tenancy.user_by_subject(session.get("sub", ""))

    async def add_contact(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        other = self.registry.tenancy.user_by_handle(str(form.get("handle", "")).strip().lstrip("@"))
        if me is None or other is None:
            return HTMLResponse(self._render_dashboard(session, error="No such user."), status_code=400)
        try:
            c = self.registry.social.request_contact(me.id, other.id)
            kind = "contact_accepted" if c.status == "accepted" else "contact_request"
            self.registry.social.create_notification(other.id, kind, f"@{me.handle} wants to connect")
            return HTMLResponse(self._render_dashboard(session, notice=f"Request sent to @{other.handle}."))
        except SocialError as exc:
            return HTMLResponse(self._render_dashboard(session, error=str(exc)), status_code=400)

    async def accept_contact(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        other = self.registry.tenancy.user_by_handle(str(form.get("handle", "")).strip().lstrip("@"))
        if me is None or other is None:
            return HTMLResponse(self._render_dashboard(session, error="No such user."), status_code=400)
        try:
            self.registry.social.accept_contact(me.id, other.id)
            self.registry.social.create_notification(other.id, "contact_accepted", f"@{me.handle} accepted your request")
            return HTMLResponse(self._render_dashboard(session, notice=f"You're now connected with @{other.handle}."))
        except SocialError as exc:
            return HTMLResponse(self._render_dashboard(session, error=str(exc)), status_code=400)

    async def read_notifications(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        if me is not None:
            self.registry.social.mark_notifications_read(me.id)
        return HTMLResponse(self._render_dashboard(session, notice="Notifications cleared."))

    async def save_profile(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        handle = self._account_handle(session)
        if handle is None:
            return PlainTextResponse("No account.", status_code=403)
        form = await request.form()
        try:
            self.registry.tenancy.set_profile(
                handle,
                display_name=str(form.get("display_name", "")),
                avatar_url=str(form.get("avatar_url", "")),
            )
            return HTMLResponse(self._render_dashboard(session, notice="Profile saved."))
        except TenancyError as exc:
            return HTMLResponse(self._render_dashboard(session, error=str(exc)), status_code=400)

    # -- extension notification API (bearer-token, for the Chrome notifier) ---

    _EXT_TOKEN_TTL = 30 * 24 * 3600  # scope='notify' only — cannot act as a session

    def _bearer_user(self, request: "Request"):
        """Resolve the Authorization: Bearer <extension token> to a tenancy user, or None.
        Requires scope='notify' — a session cookie can't be replayed on this API, nor
        this token replayed as a session."""
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        claims = self.auth.verify(auth[7:].strip(), expected_scope="notify")
        if claims is None:
            return None
        return self.registry.tenancy.user_by_subject(claims.get("sub", ""))

    async def api_notifications(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        user = self._bearer_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        social = self.registry.social
        notes = social.list_notifications(user.id, unread_only=True)
        counts = social.unread_counts(user.id)
        return JSONResponse({
            "ok": True,
            "unread": [{"id": n.id, "kind": n.kind, "body": n.body, "at": n.created} for n in notes],
            "counts": counts,
        })

    async def api_mark_read(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        user = self._bearer_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        self.registry.social.mark_notifications_read(user.id)
        return JSONResponse({"ok": True})

    def _logged_in_redirect(self, subject: str, email_or_login: str, handle: str) -> "Response":
        resp = RedirectResponse("/dashboard", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE,
            self.auth.issue(subject, email_or_login, handle),
            max_age=self.settings.dashboard_session_ttl_hours * 3600,
            httponly=True, secure=True, samesite="lax", path="/",
        )
        return resp

    # -- rendering -----------------------------------------------------------

    def _page(self, title: str, body: str) -> str:
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_html.escape(title)} — Engram</title>"
            "<style>"
            "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
            "max-width:640px;margin:0 auto;padding:2rem 1.25rem;line-height:1.55;"
            "color:#2a2320;background:#faf6ee}"
            "h1{font-size:1.5rem}a{color:#c26a3f}input,button{font:inherit;padding:.55rem .7rem;"
            "border-radius:8px;border:1px solid #d8c9ad}button{background:#c26a3f;color:#fff;border:0;cursor:pointer}"
            ".card{background:#fff;border:1px solid #e7dcc6;border-radius:12px;padding:1rem 1.15rem;margin:.9rem 0}"
            ".mono{font-family:ui-monospace,Menlo,Consolas,monospace;background:#f1e9d9;padding:.15rem .35rem;border-radius:5px}"
            ".notice{background:#e7f3e8;border:1px solid #b7dcbb;color:#2f6a35;padding:.6rem .8rem;border-radius:8px}"
            ".error{background:#fbe7de;border:1px solid #e6b29a;color:#8a3a18;padding:.6rem .8rem;border-radius:8px}"
            "@media(prefers-color-scheme:dark){body{background:#171310;color:#e9e0d2}"
            ".card{background:#211b16;border-color:#3a2f24}.mono{background:#2a2018}}"
            "</style></head><body>"
            f"<h1>{_html.escape(title)}</h1>{body}</body></html>"
        )

    def _connect_block(self, handle: str) -> str:
        url = f"{self.settings.public_url}/mcp"
        return (
            "<div class=card><h2>Connect your AI</h2>"
            f"<p>Your handle is <span class=mono>@{_html.escape(handle)}</span>. "
            "Add Engram as an MCP connector:</p>"
            f"<p><b>claude.ai / mobile:</b> Settings → Connectors → Add custom connector → "
            f"<span class=mono>{_html.escape(url)}</span> → sign in.</p>"
            f"<p><b>Claude Code:</b> <span class=mono>claude mcp add --transport http engram {_html.escape(url)}</span></p>"
            f"<p><b>ChatGPT:</b> Settings → Connectors → add <span class=mono>{_html.escape(url)}</span> "
            "(needs a plan with custom MCP connectors).</p></div>"
        )

    def _render_login(self, next_kind: str, invite_token: str | None, ext_redirect: str | None = None) -> str:
        from urllib.parse import quote

        q = f"?kind={next_kind}"
        if invite_token:
            q += f"&token={_html.escape(invite_token)}"
        if ext_redirect:
            q += f"&redirect={_html.escape(quote(ext_redirect, safe=''))}"
        buttons = "".join(
            f"<p><a class=mono href='/dashboard/auth/{name}{q}'>Sign in with "
            f"{name.capitalize()}</a></p>"
            for name in self.idps
        )
        lead = "<p>Sign in to connect the notifier extension.</p>" if next_kind == "ext" else ""
        return self._page("Sign in", f"{lead}<div class=card>{buttons or '<p>No sign-in method configured.</p>'}</div>")

    def _render_claim(self, suggested: str, email: str, error: str | None = None) -> str:
        # Suggest a handle from the login/email local-part, sanitized.
        seed = suggested.split("@", 1)[0].lower()
        safe = "".join(c for c in seed if c.isalnum() or c == "-").strip("-")[:32] or "me"
        err = f"<p class=error>{_html.escape(error)}</p>" if error else ""
        return self._page(
            "Claim your handle",
            f"<p>You're accepting an invite for <b>{_html.escape(email)}</b>.</p>{err}"
            "<form method=post action='/join/claim'><div class=card>"
            "<p>Pick a handle (lowercase letters, digits, hyphens):</p>"
            f"<p>@ <input name=handle value='{_html.escape(safe)}' "
            "pattern='[a-z0-9-]{2,32}' required></p>"
            "<button type=submit>Create my brain</button></div></form>",
        )

    def _render_dashboard(self, session: dict, notice: str | None = None, error: str | None = None,
                          projects: list | None = None) -> str:
        handle = self._account_handle(session) or "?"
        banner = (f"<p class=notice>{_html.escape(notice)}</p>" if notice else "") + (
            f"<p class=error>{_html.escape(error)}</p>" if error else ""
        )
        body = [banner, self._render_profile_block(session, handle)]
        if projects is not None:
            body.append(self._render_projects_block(projects))
        body.append(self._render_social_block(session))
        body.append(self._connect_block(handle))
        body.append(self._render_extension_block(session, handle))
        if self._is_owner(session):
            body.append(self._render_admin())
        body.append("<div class=card><form method=post action='/dashboard/logout'>"
                    "<button type=submit>Sign out</button></form></div>")
        return self._page(f"Welcome, @{handle}", "".join(body))

    def _avatar_img(self, user, size: int = 40) -> str:
        """A safe <img> for a user's avatar (escaped https URL), or a letter fallback."""
        if user is not None and user.avatar_url:
            return (
                f"<img src='{_html.escape(user.avatar_url, quote=True)}' "
                f"width={size} height={size} alt='' "
                f"style='border-radius:50%;object-fit:cover;vertical-align:middle'>"
            )
        letter = (user.handle[0].upper() if user else "?")
        return (
            f"<span style='display:inline-flex;width:{size}px;height:{size}px;border-radius:50%;"
            "background:#c26a3f;color:#fff;align-items:center;justify-content:center;"
            f"font-weight:600;vertical-align:middle'>{_html.escape(letter)}</span>"
        )

    def _render_profile_block(self, session: dict, handle: str) -> str:
        user = self.registry.tenancy.user_by_handle(handle)
        name = (user.display_name if user else "") or ""
        avatar = (user.avatar_url if user else "") or ""
        return (
            "<div class=card><h2>Your profile</h2>"
            f"<p>{self._avatar_img(user)} <span class=mono>@{_html.escape(handle)}</span></p>"
            "<form method=post action='/dashboard/profile'>"
            "<p>Display name<br><input name=display_name maxlength=60 "
            f"value='{_html.escape(name)}' placeholder='e.g. Amiyanshu'></p>"
            "<p>Avatar image URL (https)<br><input name=avatar_url style='width:100%' "
            f"value='{_html.escape(avatar)}' placeholder='https://…/me.png'></p>"
            "<button type=submit>Save profile</button></form></div>"
        )

    def _render_social_block(self, session: dict) -> str:
        me = self._session_user(session)
        if me is None:
            return ""
        social = self.registry.social
        hmap = self.registry.tenancy_handle_map()
        graph = social.list_contacts(me.id)
        counts = social.unread_counts(me.id)
        notes = social.list_notifications(me.id, unread_only=True)

        def _h(uid):
            return _html.escape(hmap.get(uid, str(uid)))

        contacts = ", ".join(f"@{_h(i)}" for i in graph["accepted"]) or "none yet"
        incoming = "".join(
            f"<li>@{_h(i)} "
            "<form style='display:inline' method=post action='/dashboard/contact/accept'>"
            f"<input type=hidden name=handle value='{_h(i)}'>"
            "<button type=submit>accept</button></form></li>"
            for i in graph["incoming"]
        ) or "<li>none</li>"
        outgoing = ", ".join(f"@{_h(i)}" for i in graph["outgoing"]) or "none"
        notif_items = "".join(
            f"<li>{_html.escape(n.body)} <span style='opacity:.6;font-size:12px'>{_html.escape(n.created)}</span></li>"
            for n in notes
        ) or "<li>No unread notifications.</li>"
        clear = (
            "<form method=post action='/dashboard/notifications/read'>"
            "<button type=submit>Mark all read</button></form>" if notes else ""
        )
        return (
            "<div class=card><h2>Notifications"
            + (f" <span class=mono>{len(notes)}</span>" if notes else "")
            + f"</h2><ul>{notif_items}</ul>{clear}"
            + (f"<p style='font-size:13px;color:#8a7960'>{counts.get('dms', 0)} unread DM(s) — "
               "read them from your Claude with kb_messages.</p>" if counts.get("dms") else "")
            + "</div>"
            "<div class=card><h2>Contacts</h2>"
            f"<p>{contacts}</p>"
            f"<p><b>Requests to accept:</b></p><ul>{incoming}</ul>"
            f"<p style='font-size:13px;color:#8a7960'>Sent, awaiting: {outgoing}</p>"
            "<form method=post action='/dashboard/contact/add'>"
            "<input name=handle placeholder='@handle' required> "
            "<button type=submit>Add contact</button></form></div>"
        )

    # -- brain rendering (scoped to the caller's own brain) ------------------

    def _md(self, content: str, path: str) -> str:
        """Render a concept's markdown body; internal links point at THIS home's
        /dashboard/f/ (not the operator explorer's /brain/f/)."""
        from engram_server.explorer.render import render_markdown, split_frontmatter

        _meta, body = split_frontmatter(content)
        html = render_markdown(body, path)
        return html.replace('href="/brain/f/', 'href="/dashboard/f/')

    def _render_projects_block(self, projects: list) -> str:
        if not projects:
            return ("<div class=card><h2>Your projects</h2>"
                    "<p style='color:#8a7960'>No projects yet — start one from your Claude "
                    "(<span class=mono>kb_write</span>), and it'll show here.</p></div>")
        cards = "".join(
            f"<li style='margin:.5rem 0'><a href='/dashboard/p/{_html.escape(str(p['id']))}'>"
            f"<b>{_html.escape(str(p.get('title') or p['id']))}</b></a>"
            + (f" — {_html.escape(str(p['description']))}" if p.get("description") else "")
            + (f" <span style='color:#8a7960;font-size:12px'>· {_html.escape(str(p['last_session']))}</span>"
               if p.get("last_session") else "")
            + "</li>"
            for p in projects
        )
        return f"<div class=card><h2>Your projects</h2><ul style='list-style:none;padding:0'>{cards}</ul></div>"

    def _tree_html(self, node: dict) -> str:
        """Recursive nav list of a project's concepts, linking to /dashboard/f/."""
        parts = []
        for f in node.get("files", []):
            label = f.get("title") or f.get("name")
            parts.append(
                f"<li><a href='/dashboard/f/{_html.escape(f['path'])}'>{_html.escape(str(label))}</a>"
                + (f" <span style='color:#8a7960;font-size:12px'>— {_html.escape(str(f['description']))}</span>"
                   if f.get("description") else "")
                + "</li>"
            )
        for d in node.get("dirs", []):
            inner = self._tree_html(d)
            if inner.strip():
                parts.append(f"<li><b>{_html.escape(d.get('title') or d['name'])}/</b>{inner}</li>")
        return f"<ul>{''.join(parts)}</ul>" if parts else ""

    def _render_project(self, project: str, data: dict) -> str:
        ctx = data.get("context_md") or ""
        log_entries = data.get("recent_log") or []
        tree = data.get("index_tree") or {}
        body = [f"<p><a href='/dashboard'>&larr; home</a></p>"]
        if ctx:
            body.append(f"<div class=card>{self._md(ctx, f'{project}/context.md')}</div>")
        if log_entries:
            logs = "".join(f"<div class=card>{self._md(e, f'{project}/log.md')}</div>"
                           for e in log_entries[:3])
            body.append(f"<h2>Recent log</h2>{logs}")
        tree_html = self._tree_html(tree)
        if tree_html.strip():
            body.append(f"<div class=card><h2>Concepts</h2>{tree_html}</div>")
        return self._page(str(project), "".join(body))

    def _render_concept(self, path: str, concept: dict) -> str:
        title = str((concept.get("meta") or {}).get("title") or path.rsplit("/", 1)[-1])
        # breadcrumb from the path
        parts = path.split("/")
        crumbs = ["<a href='/dashboard'>home</a>"]
        if len(parts) > 1:
            crumbs.append(f"<a href='/dashboard/p/{_html.escape(parts[0] if parts[0] != 'projects' else parts[1])}'>"
                          f"{_html.escape(parts[1] if parts[0] == 'projects' and len(parts) > 1 else parts[0])}</a>")
        body = (
            f"<p style='font-size:13px;color:#8a7960'>{' / '.join(crumbs)}</p>"
            f"<div class=card>{self._md(concept.get('content') or '', path)}</div>"
        )
        return self._page(title, body)

    def _render_extension_block(self, session: dict, handle: str) -> str:
        """The long-lived bearer token the Chrome notifier extension pastes into its options."""
        token = self.auth.issue(
            session["sub"], session.get("email", ""), handle,
            ttl=self._EXT_TOKEN_TTL, scope="notify",
        )
        return (
            "<div class=card><h2>Desktop notifications</h2>"
            "<p>Install the Engram Chrome extension, open its Options, and click "
            "<b>Sign in with Engram</b> — same login as your Claude connector, no token to copy. "
            "It'll ping you when you get a DM.</p>"
            "<details><summary style='cursor:pointer;font-size:13px'>Advanced: paste a token instead</summary>"
            f"<p><span class=mono style='word-break:break-all'>{_html.escape(token)}</span></p>"
            "<p style='font-size:13px;color:#8a7960'>Treat this like a password — anyone "
            "with it can read your notifications. Rotate by asking the operator to reset "
            "the server secret.</p></details></div>"
        )

    def _render_admin(self) -> str:
        users = self.registry.tenancy.list_users()
        invites = self.registry.tenancy.list_invites(live_only=True)
        rows = "".join(
            f"<li style='margin:.4rem 0'>{self._avatar_img(u, 28)} "
            f"<span class=mono>@{_html.escape(u.handle)}</span>"
            + (f" · {_html.escape(u.display_name)}" if u.display_name else "")
            + f" — {_html.escape(u.email)} ({_html.escape(u.status)})</li>"
            for u in users
        ) or "<li>No members yet.</li>"
        inv_rows = "".join(
            f"<li>{_html.escape(i.email)} "
            f"<form style='display:inline' method=post action='/dashboard/invite/revoke'>"
            f"<input type=hidden name=token value='{_html.escape(i.token)}'>"
            "<button type=submit>revoke</button></form></li>"
            for i in invites
        ) or "<li>No pending invites.</li>"
        return (
            "<div class=card><h2>Members</h2><ul>" + rows + "</ul></div>"
            "<div class=card><h2>Invite someone</h2>"
            "<p style='font-size:13px;color:#8a7960'>By email (they sign in with Google or GitHub):</p>"
            "<form method=post action='/dashboard/invite'>"
            "<p><input name=email type=email placeholder='colleague@example.com' required> "
            "<button type=submit>Send invite</button></p></form>"
            "<p style='font-size:13px;color:#8a7960'>Or by GitHub username (no email — you share the link; "
            "only that GitHub account can accept):</p>"
            "<form method=post action='/dashboard/invite/github'>"
            "<p><input name=login placeholder='octocat' required> "
            "<button type=submit>Invite GitHub user</button></p></form>"
            "<h3>Pending invites</h3><ul>" + inv_rows + "</ul></div>"
        )


def register_dashboard(mcp, settings: "Settings", registry: "StoreRegistry", idps: dict) -> Dashboard | None:
    """Wire the dashboard + onboarding routes. No-op (returns None) unless multiuser
    is on and at least one IdP is configured — single-user deployments never expose it."""
    if not settings.multiuser or not idps:
        return None
    dash = Dashboard(settings, registry, idps)

    mcp.custom_route("/dashboard", ["GET"])(dash.dashboard)
    mcp.custom_route("/dashboard/login", ["GET"])(dash.login)
    mcp.custom_route("/dashboard/auth/{idp}", ["GET"])(dash.start_oauth)
    mcp.custom_route(CALLBACK_PATH, ["GET"])(dash.callback)
    mcp.custom_route("/join", ["GET"])(dash.join)
    mcp.custom_route("/join/claim", ["POST"])(dash.claim)
    mcp.custom_route("/dashboard/invite", ["POST"])(dash.create_invite)
    mcp.custom_route("/dashboard/invite/github", ["POST"])(dash.create_github_invite)
    mcp.custom_route("/dashboard/invite/revoke", ["POST"])(dash.revoke_invite)
    mcp.custom_route("/dashboard/logout", ["POST"])(dash.logout)
    mcp.custom_route("/dashboard/profile", ["POST"])(dash.save_profile)
    mcp.custom_route("/dashboard/p/{project}", ["GET"])(dash.browse_project)
    mcp.custom_route("/dashboard/f/{path:path}", ["GET"])(dash.browse_concept)
    mcp.custom_route("/dashboard/contact/add", ["POST"])(dash.add_contact)
    mcp.custom_route("/dashboard/contact/accept", ["POST"])(dash.accept_contact)
    mcp.custom_route("/dashboard/notifications/read", ["POST"])(dash.read_notifications)
    mcp.custom_route("/dashboard/api/notifications", ["GET"])(dash.api_notifications)
    mcp.custom_route("/dashboard/api/notifications/read", ["POST"])(dash.api_mark_read)
    mcp.custom_route("/dashboard/ext-auth", ["GET"])(dash.ext_auth)
    return dash
