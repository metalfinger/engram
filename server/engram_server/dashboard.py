"""Dashboard + onboarding (v2 M1.4) — the browser front-of-house for multi-user Engram.

Three surfaces sharing ONE signed-cookie session (separate from the MCP
bearer-token OAuth; since v3 "one door" the legacy explorer verifies this SAME
cookie — there is no second identity system):

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

import asyncio
import html as _html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

log = logging.getLogger("engram.dashboard")

# Must match app._THREAD_FLOOR_PREFIX — a thread's floor lives in a hidden
# room under this prefix, and both surfaces have to agree on the name.
_THREAD_FLOOR_PREFIX = "thread--"
from urllib.parse import urlsplit

import httpx
import jwt
from anyio import to_thread
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from engram_server.discovery import DiscoveryError
from engram_server.kbstore import _as_tags as _tags_of
from engram_server.kbstore import _scan_secrets
from engram_server.errors import KBError
from engram_server.explorer import social_pages
from engram_server.provisioning import ensure_user_brain
from engram_server.social import SocialError
from engram_server.teamwork import TeamworkError
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
    kind: str  # "login" | "join" | "signup" | "ext"
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
        # Fire-and-forget notification-push tasks (mirrors app.py's _social_bg) — a
        # bare reference set so asyncio doesn't garbage-collect a task mid-flight.
        self._bg: set = set()

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

    def _push_notification(self, user_id: int, kind: str, body: str, ref: str | None = None) -> None:
        """Persist a notification and fan it out (email/telegram) best-effort, off the
        caller's critical path. Duplicates app.py's ``_push_notification`` idiom
        (dashboard.py may not import app.py) — fanout never raises; a failed push
        never fails the request."""
        self.registry.social.create_notification(user_id, kind, body, ref)
        user = next((u for u in self.registry.tenancy.list_users() if u.id == user_id), None)
        if user is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        from engram_server import notify as _notify

        recipient = _notify.Recipient(handle=user.handle, email=user.email, telegram_chat_id=None, prefs={})
        task = loop.create_task(
            _notify.fanout(self.settings, recipient, {"kind": kind, "body": body, "ref": ref})
        )
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # -- route handlers ------------------------------------------------------

    def _session(self, request: "Request") -> dict | None:
        return self.auth.verify(request.cookies.get(SESSION_COOKIE))

    async def dashboard(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        projects = await self._user_projects(session)
        return HTMLResponse(await self._render_dashboard(session, projects=projects))

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
        # Folder-aware resolution: 'engram' may live at projects/personal/engram —
        # the store's own resolver knows; hand-building 'projects/<id>' 404'd every
        # foldered project on the web.
        rel = None
        if re.fullmatch(r"[a-z0-9-]+", project):
            if project == "metalfinger":
                rel = "metalfinger"
            else:
                try:
                    rel = store._project_rel(project)
                except KBError:
                    rel = None
        if rel is None or not (store.root / rel).is_dir():
            return HTMLResponse(self._brain_shell("Not found",
                "<p class='empty'>No such project.</p>", [("home", "/dashboard")],
                self._brain_sidebar(await self._user_projects(session))), status_code=404)
        projects = await self._user_projects(session)
        return HTMLResponse(self._render_project(store, project, rel, projects))

    async def move_project_action(self, request: "Request") -> "Response":
        from urllib.parse import quote

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        project = str(request.path_params.get("project", ""))
        if store is None or not re.fullmatch(r"[a-z0-9-]+", project):
            return RedirectResponse("/dashboard", status_code=302)
        form = await request.form()
        folder = str(form.get("folder", "")).strip()
        try:
            await store.kb_move_project(project, folder)
        except KBError as exc:
            return RedirectResponse(f"/dashboard/p/{project}?error={quote(str(exc))}", status_code=302)
        return RedirectResponse(f"/dashboard/p/{project}", status_code=302)

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
        except KBError:
            return HTMLResponse(self._brain_shell("Not found",
                "<p class='empty'>No such concept.</p>", [("home", "/dashboard")],
                self._brain_sidebar(await self._user_projects(session))), status_code=404)
        projects = await self._user_projects(session)
        return HTMLResponse(self._render_concept(store, path, concept, projects))

    # -- social/discovery web pages (M5) -------------------------------------

    async def _public_work_for(self, handle: str) -> list:
        try:
            store = await self.registry.store_for_handle(handle)
            return (await store.kb_public()).get("public", [])
        except (KBError, Exception):  # noqa: BLE001 — a browse convenience
            return []

    def _profile_for(self, handle: str, viewer_id: int | None) -> dict | None:
        user = self.registry.tenancy.user_by_handle(handle.lstrip("@"))
        if user is None:
            return None
        counts = self.registry.discovery.follow_counts(user.id)
        return {
            "handle": user.handle, "display_name": user.display_name,
            "avatar_url": user.avatar_url, "bio": user.bio,
            "followers": counts.get("followers", 0), "following": counts.get("following", 0),
            "is_following": bool(viewer_id and self.registry.discovery.is_following(viewer_id, user.id)),
        }

    async def _social_shell(self, session: dict, title: str, body: str, crumbs, active_tab: str = "") -> str:
        projects = await self._user_projects(session)
        return self._brain_shell(title, body, crumbs, self._brain_sidebar(projects), active_tab=active_tab,
                                 is_owner=self._is_owner(session), handle=self._account_handle(session) or "")

    def _render_working_strip(self) -> str:
        """The "Working now" strip: who's active, in what project, how fresh.
        Shared by /dashboard/people and /dashboard/office (both team-facing views)."""
        from engram_server.explorer.html import esc

        umap = {u.id: u for u in self.registry.tenancy.list_users()}
        rows = self.registry.presence.roster()
        if not rows:
            return ""
        cards = []
        for row in rows:
            u = umap.get(row["user_id"])
            if u is None:
                continue
            dot = "🟢" if row["minutes_ago"] <= 15 else ("🟠" if row["minutes_ago"] <= 120 else "⚪")
            cards.append(
                "<div class='card' style='display:flex;align-items:center;gap:.6rem'>"
                f"{self._avatar_for(u.handle, u.display_name or '', u.avatar_url or '', 32)}"
                f"<div><p class='meta'>{dot} <a href='/dashboard/u/{esc(u.handle)}'>@{esc(u.handle)}</a>"
                + (f" in {esc(row['project'])}" if row.get("project") else "")
                + f"<br><span style='font-size:11px'>{esc(row.get('tool') or '')}</span></p></div></div>"
            )
        return "<p class='section-label'>Working now</p><div class='cards'>" + "".join(cards) + "</div>"

    async def _render_working_on(self, handle: str) -> str:
        """"Working on" — which PATHS are being worked, by whom, right now.

        Sessions have had this in their `floor` block since the coordination wave;
        Hiren has had no view of it at all short of asking one of them. He is the
        one person who cannot be handed a tool result, so the surface he actually
        opens has to carry it.

        Claims are stated intent, activity is observed writes; the distinction is
        shown rather than flattened, because "I'm taking this" and "they have been
        editing this" warrant different reactions."""
        from engram_server.explorer.html import esc

        rows: list[str] = []
        try:
            claims = await (await self.registry.store_for_handle(handle)).kb_claims()
        except Exception:  # noqa: BLE001 — a panel must never break the page
            log.debug("claims unavailable for the working-on panel", exc_info=True)
            claims = []
        claimed: set[str] = set()
        for c in claims:
            if c.get("stale"):
                continue
            path = str(c.get("path") or "")
            claimed.add(path)
            note = str(c.get("note") or "")
            rows.append(
                "<tr><td><code>" + esc(path) + "</code></td>"
                f"<td>{esc(str(c.get('session') or ''))}</td>"
                "<td><span class='badge'>claimed</span></td>"
                f"<td class='meta'>{esc(note)}</td>"
                f"<td class='meta'>{int(c.get('age_min') or 0)}m ago</td></tr>"
            )
        try:
            for a in self.registry.rooms.recent_activity():
                if a["path"] in claimed:
                    continue
                rows.append(
                    "<tr><td><code>" + esc(str(a["path"])) + "</code></td>"
                    f"<td>{esc(str(a['session']))}</td>"
                    "<td><span class='badge'>writing</span></td>"
                    "<td class='meta'></td>"
                    f"<td class='meta'>{esc(str(a['at']))}</td></tr>"
                )
        except Exception:  # noqa: BLE001
            pass
        if not rows:
            return ""
        return (
            "<p class='section-label'>Working on</p>"
            "<table class='rows'><thead><tr><th>path</th><th>session</th>"
            "<th>state</th><th>note</th><th>when</th></tr></thead><tbody>"
            + "".join(rows[:20]) + "</tbody></table>"
        )

    async def people_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        people = []
        for u in self.registry.tenancy.list_users():
            if u.status != "active":
                continue
            work = await self._public_work_for(u.handle)
            if not work and not (me and u.id == me.id):
                continue
            counts = self.registry.discovery.follow_counts(u.id)
            people.append({
                "handle": u.handle, "display_name": u.display_name, "avatar_url": u.avatar_url,
                "bio": u.bio, "followers": counts.get("followers", 0),
                "public_projects": len({i.get("project") for i in work if i.get("project")}),
                "is_following": bool(me and self.registry.discovery.is_following(me.id, u.id)),
            })
        body = (self._subtabs(self._PEOPLE_SUBTABS, "/dashboard/people")
                + self._render_working_strip() + social_pages.people_body(people, me.handle if me else ""))
        return HTMLResponse(await self._social_shell(session, "People", body,
                                                     [("home", "/dashboard"), ("people", "/dashboard/people")],
                                                     active_tab="people"))

    async def profile_view(self, request: "Request") -> "Response":
        from engram_server.explorer.html import esc

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        handle = str(request.path_params.get("handle", ""))
        profile = self._profile_for(handle, me.id if me else None)
        if profile is None:
            return HTMLResponse(await self._social_shell(session, "Not found",
                "<p class='empty'>No such person.</p>", [("home", "/dashboard")]), status_code=404)
        work = await self._public_work_for(profile["handle"])
        room_btn = ""
        if me is not None and profile["handle"] != me.handle:
            room_btn = (
                "<form method='post' action='/dashboard/rooms/open-with'>"
                f"<input type='hidden' name='handle' value='{esc(profile['handle'])}'>"
                f"<button type='submit'>Open a room with @{esc(profile['handle'])}</button></form>"
            )
        body = social_pages.profile_body(profile, work, me.handle if me else "") + room_btn
        return HTMLResponse(await self._social_shell(
            session, f"@{profile['handle']}", body,
            [("home", "/dashboard"), ("people", "/dashboard/people"), (profile["handle"], f"/dashboard/u/{profile['handle']}")],
            active_tab="people"))

    async def public_concept_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        handle = str(request.path_params.get("handle", ""))
        path = str(request.path_params.get("path", ""))
        owner = self.registry.tenancy.user_by_handle(handle.lstrip("@"))
        if owner is None:
            return HTMLResponse(await self._social_shell(session, "Not found",
                "<p class='empty'>No such person.</p>", [("home", "/dashboard")]), status_code=404)
        try:
            store = await self.registry.store_for_handle(owner.handle)
            if await store.effective_visibility(path) != "public":
                raise KBError("not public")
            concept = await store.kb_read(path, depth=0)
        except KBError:
            return HTMLResponse(await self._social_shell(session, "Not available",
                "<p class='empty'>That isn't published.</p>",
                [("home", "/dashboard"), (owner.handle, f"/dashboard/u/{owner.handle}")]), status_code=404)
        from engram_server.explorer.render import render_markdown_public, split_frontmatter

        meta, body_md = split_frontmatter(concept.get("content") or "")
        title = str(meta.get("title") or path.rsplit("/", 1)[-1])
        body = social_pages.public_concept_body(
            owner.handle, path, title, meta, render_markdown_public(body_md), me.handle if me else "")
        return HTMLResponse(await self._social_shell(
            session, title, body,
            [("home", "/dashboard"), (owner.handle, f"/dashboard/u/{owner.handle}"), (title, "#")]))

    async def feed_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        items = []
        if me is not None:
            umap = {u.id: u for u in self.registry.tenancy.list_users()}
            for uid in self.registry.discovery.following(me.id):
                u = umap.get(uid)
                if u is None:
                    continue
                for item in await self._public_work_for(u.handle):
                    items.append({**item, "handle": u.handle, "display_name": u.display_name,
                                  "avatar_url": u.avatar_url})
            items.sort(key=lambda i: str(i.get("updated") or ""), reverse=True)
        body = self._subtabs(self._PEOPLE_SUBTABS, "/dashboard/feed") + social_pages.feed_body(items[:50])
        return HTMLResponse(await self._social_shell(session, "Feed", body,
                                                     [("home", "/dashboard"), ("feed", "/dashboard/feed")],
                                                     active_tab="people"))

    async def asks_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        to_answer, i_asked = [], []
        if me is not None:
            umap = {u.id: u for u in self.registry.tenancy.list_users()}

            def _h(uid):
                u = umap.get(uid)
                return u.handle if u else str(uid)

            to_answer = [
                {"id": a.id, "from_handle": _h(a.asker_id), "to_handle": me.handle, "path": a.path,
                 "question": a.question, "answer": a.answer, "status": a.status,
                 "created": a.created, "answered_at": a.answered_at}
                for a in self.registry.discovery.list_asks_for(me.id, open_only=False)
            ]
            i_asked = [
                {"id": a.id, "from_handle": me.handle, "to_handle": _h(a.owner_id), "path": a.path,
                 "question": a.question, "answer": a.answer, "status": a.status,
                 "created": a.created, "answered_at": a.answered_at}
                for a in self.registry.discovery.list_asks_by(me.id)
            ]
        body = (self._subtabs(self._PEOPLE_SUBTABS, "/dashboard/asks")
                + social_pages.asks_body(to_answer, i_asked, me.handle if me else ""))
        return HTMLResponse(await self._social_shell(session, "Questions", body,
                                                     [("home", "/dashboard"), ("questions", "/dashboard/asks")],
                                                     active_tab="people"))

    async def follow_action(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        handle = str(form.get("handle", "")).strip().lstrip("@")
        other = self.registry.tenancy.user_by_handle(handle)
        if me is not None and other is not None and other.id != me.id:
            if str(form.get("follow", "1")) == "1":
                self.registry.discovery.follow(me.id, other.id)
                self.registry.social.create_notification(other.id, "new_follower", f"@{me.handle} followed you")
            else:
                self.registry.discovery.unfollow(me.id, other.id)
        return RedirectResponse(self._safe_back(request, "/dashboard/people"), status_code=302)

    @staticmethod
    def _safe_back(request: "Request", fallback: str) -> str:
        """Where to send the user after a form POST, without becoming an open redirect.

        The Referer is attacker-influenceable (a form on evil.com can send one), so we
        never redirect to it directly: we require the SAME ORIGIN and then use only its
        PATH. Substring checks like '/dashboard' in referer are unsafe —
        https://evil.com/dashboard would pass one — and a bare startswith('/') would
        still allow the protocol-relative //evil.com."""
        from urllib.parse import urlsplit

        ref = request.headers.get("referer") or ""
        if not ref:
            return fallback
        parts = urlsplit(ref)
        same_origin = parts.netloc == urlsplit(str(request.url)).netloc
        if not same_origin or not parts.path.startswith("/dashboard"):
            return fallback
        return parts.path + (f"?{parts.query}" if parts.query else "")

    async def ask_action(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        handle = str(form.get("handle", "")).strip().lstrip("@")
        path = str(form.get("path", "")).strip()
        question = str(form.get("question", "")).strip()
        owner = self.registry.tenancy.user_by_handle(handle)
        if me is None or owner is None:
            return RedirectResponse("/dashboard/people", status_code=302)
        try:
            store = await self.registry.store_for_handle(owner.handle)
            if await store.effective_visibility(path) != "public":
                raise KBError("not public")
            ask = self.registry.discovery.create_ask(me.id, owner.id, path, question)
            self.registry.social.create_notification(
                owner.id, "question", f"@{me.handle} asked about {path}", str(ask.id))
        except (KBError, DiscoveryError):
            pass
        return RedirectResponse(f"/dashboard/u/{owner.handle}/f/{path}", status_code=302)

    async def answer_action(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        try:
            ask = self.registry.discovery.answer_ask(
                int(form.get("ask_id", 0)), me.id, str(form.get("answer", "")))
            self.registry.social.create_notification(
                ask.asker_id, "answer", f"@{me.handle} answered your question", str(ask.id))
        except (DiscoveryError, ValueError, TypeError, AttributeError):
            pass
        return RedirectResponse("/dashboard/asks", status_code=302)

    async def browse_search(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        if store is None:
            return HTMLResponse(self._page("No account", "<p>No brain to search.</p>"), status_code=403)
        q = str(request.query_params.get("q", "")).strip()
        projects = await self._user_projects(session)
        results = []
        if q:
            try:
                res = await store.kb_search(q)
                results = res.get("results", res) if isinstance(res, dict) else res
            except Exception:  # noqa: BLE001
                results = []
        return HTMLResponse(self._render_search(q, results, projects))

    async def browse_activity(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        if store is None:
            return HTMLResponse(self._page("No account", "<p>No brain.</p>"), status_code=403)
        projects = await self._user_projects(session)
        return HTMLResponse(await self._render_activity(store, projects))

    async def browse_graph(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        if store is None:
            return HTMLResponse(self._page("No account", "<p>No brain.</p>"), status_code=403)
        from anyio import to_thread as _tt

        from engram_server.explorer.format import TYPE_GLYPHS
        from engram_server.explorer.html import graph_page
        from engram_server.explorer.routes import _graph_data

        data = await _tt.run_sync(_graph_data, store.root)
        return HTMLResponse(self._graph_dashify(graph_page(data, dict(TYPE_GLYPHS))))

    async def login(self, request: "Request") -> "Response":
        return HTMLResponse(self._render_login(next_kind="login", invite_token=None))

    async def join(self, request: "Request") -> "Response":
        """Sign up. With ?token=… it redeems an invite; without one it's open signup
        (when the operator allows it)."""
        token = request.query_params.get("token", "")
        if not token:
            if not self.settings.open_signup:
                return HTMLResponse(
                    self._page("Signups are closed",
                               "<div class='card'><p>This Engram isn't accepting new accounts "
                               "right now. If someone invited you, open their invite link.</p>"
                               "<p><a href='/dashboard/login'>Already have an account? Sign in</a>"
                               "</p></div>"),
                    status_code=403,
                )
            return HTMLResponse(self._render_login(next_kind="signup", invite_token=None))
        invite = self.registry.tenancy.get_invite(token)
        if invite is None or not invite.live:
            body = ("<div class='card'><p>This invite link is invalid, already used, or "
                    "expired.</p>" + ("<p><a href='/join'>You can just sign up instead →</a></p>"
                                      if self.settings.open_signup else
                                      "<p>Ask whoever invited you for a fresh one.</p>") + "</div>")
            return HTMLResponse(self._page("Invite unavailable", body), status_code=400)
        return HTMLResponse(self._render_login(next_kind="join", invite_token=token))

    @staticmethod
    def _valid_ext_redirect(url: str) -> bool:
        """Where may a notify token be delivered? Two shapes only, so it can never
        be exfiltrated to an arbitrary host:
        - https://<extension-id>.chromiumapp.org/...   (Chrome extension)
        - http://127.0.0.1:<port>/callback/<nonce>     (desktop tray app, RFC 8252
          loopback: plain http is correct on the loopback interface; the nonce path
          keeps other local processes from squatting the callback)."""
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme == "https" and (parts.hostname or "").endswith(".chromiumapp.org"):
            return True
        return (
            parts.scheme == "http"
            and parts.hostname in ("127.0.0.1", "::1")
            and bool(re.fullmatch(r"/callback/[A-Za-z0-9_-]{8,64}", parts.path or ""))
        )

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
        if kind not in ("login", "join", "signup", "ext"):
            return PlainTextResponse("Unknown sign-in flow.", status_code=400)
        if kind == "join" and not invite_token:
            return PlainTextResponse("Missing invite token.", status_code=400)
        if kind == "signup" and not self.settings.open_signup:
            return PlainTextResponse("Signups are closed.", status_code=403)
        if kind == "ext" and not self._valid_ext_redirect(ext_redirect or ""):
            return PlainTextResponse("Invalid extension redirect.", status_code=400)
        state = secrets.token_urlsafe(24)
        self._pending[state] = _Pending(
            idp_name=idp.name, kind=kind, invite_token=invite_token, ext_redirect=ext_redirect
        )
        resp = RedirectResponse(idp.authorize_url(self.callback_url, state), status_code=302)
        # Bind this flow to THIS browser (OAuth login-CSRF / session-fixation defense):
        # the callback only proceeds if this cookie comes back matching the state param.
        # PARENT-DOMAIN scope is load-bearing (one door): the flow STARTS on the human
        # hostname (brain.*) but GitHub's registered callback lands on the machine
        # hostname (engram.*) — a host-only cookie never arrives and every sign-in
        # from brain.* dies with "could not be verified".
        resp.set_cookie(
            OAUTH_STATE_COOKIE, state,
            max_age=900, httponly=True, secure=True, samesite="lax", path="/",
            domain=self._cookie_domain(),
        )
        return resp

    async def callback(self, request: "Request") -> "Response":
        # Delete the single-use state cookie on EVERY exit (pass or fail).
        resp = await self._callback(
            request.query_params.get("state", ""),
            request.query_params.get("code", ""),
            request.cookies.get(OAUTH_STATE_COOKIE),
        )
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/", domain=self._cookie_domain())
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")  # legacy host-only leftovers
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

        if pending.kind == "signup":
            if not self.settings.open_signup:
                return HTMLResponse(self._page("Signups are closed",
                                               "<div class='card'><p>This Engram isn't accepting "
                                               "new accounts.</p></div>"), status_code=403)
            if self._account_handle({"sub": subject}) is not None:
                return self._logged_in_redirect(subject, user.login,
                                                self._account_handle({"sub": subject}))
            email = user.login if "@" in user.login else f"{user.login}@{idp.name}.local"
            resp = HTMLResponse(self._render_claim(user.login, email, signup=True))
            resp.set_cookie(
                "engram_onboarding",
                self.auth.issue(subject, email, None, ttl=900, scope="onboarding"),
                max_age=900, httponly=True, secure=True, samesite="lax", path="/join",
            )
            resp.set_cookie("engram_invite", "", max_age=900, httponly=True, secure=True,
                            samesite="lax", path="/join")
            return resp

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
                    "<div class='card'><p>That account doesn't have an Engram yet.</p>"
                    "<p><a href='/join'>Create one →</a></p></div>",
                ),
                status_code=403,
            )
        if pending.kind == "ext":
            # Hand the extension its scope='notify' token via its chromiumapp.org redirect.
            return self._ext_token_redirect(subject, user.login, handle, pending.ext_redirect)
        return self._logged_in_redirect(subject, user.login, handle)

    async def claim(self, request: "Request") -> "Response":
        onboarding = self.auth.verify(request.cookies.get("engram_onboarding"), expected_scope="onboarding")
        invite_token = request.cookies.get("engram_invite") or ""
        if onboarding is None:
            return PlainTextResponse("Sign-up session expired — start again at /join.", status_code=400)
        form = await request.form()
        handle = str(form.get("handle", "")).strip()
        idp_name = str(onboarding["sub"]).split(":", 1)[0]
        is_signup = not invite_token
        if is_signup and not self.settings.open_signup:
            return PlainTextResponse("Signups are closed.", status_code=403)
        try:
            if is_signup:
                user = self.registry.tenancy.create_account(
                    handle, onboarding["email"], idp_name, onboarding["sub"])
                await to_thread.run_sync(lambda: ensure_user_brain(self.settings, user.handle))
                claimed = user.handle
            else:
                claimed = await self._claim(invite_token, handle, idp_name, onboarding["sub"])
        except TenancyError as exc:
            return HTMLResponse(
                self._render_claim(handle, onboarding["email"], error=str(exc), signup=is_signup),
                status_code=400)
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
            return HTMLResponse(await self._render_dashboard(session, error=str(exc)), status_code=400)
        note = (
            f"Invite emailed to {email}." if result["mail"].get("sent")
            else f"Invite created — email is off, copy this link: {result['join_url']}"
        )
        return HTMLResponse(await self._render_dashboard(session, notice=note))

    async def create_github_invite(self, request: "Request") -> "Response":
        """Invite by GitHub username — bound to that GitHub identity, no email needed.
        The owner shares the returned link; only that GitHub account can accept."""
        session = self._session(request)
        if session is None or not self._is_owner(session):
            return PlainTextResponse("Only the operator can send invites.", status_code=403)
        form = await request.form()
        login = re.sub(r"[^a-zA-Z0-9-]", "", str(form.get("login", "")).strip().lstrip("@")).lower()
        if not login:
            return HTMLResponse(await self._render_dashboard(session, error="Enter a GitHub username."), status_code=400)
        inviter = self.registry.tenancy.user_by_subject(session.get("sub", ""))
        try:
            invite = self.registry.tenancy.create_invite(
                f"{login}@users.noreply.github.com",
                invited_by=inviter.id if inviter else None,
                bind_subject=f"github:{login}",
            )
        except TenancyError as exc:
            return HTMLResponse(await self._render_dashboard(session, error=str(exc)), status_code=400)
        join_url = f"{self.settings.public_url}/join?token={invite.token}"
        note = f"Invite for GitHub @{login} — send them this link: {join_url}"
        return HTMLResponse(await self._render_dashboard(session, notice=note))

    async def revoke_invite(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None or not self._is_owner(session):
            return PlainTextResponse("Only the operator can revoke invites.", status_code=403)
        form = await request.form()
        try:
            self.registry.tenancy.revoke_invite(str(form.get("token", "")))
            return HTMLResponse(await self._render_dashboard(session, notice="Invite revoked."))
        except TenancyError as exc:
            return HTMLResponse(await self._render_dashboard(session, error=str(exc)), status_code=400)

    async def logout(self, request: "Request") -> "Response":
        resp = RedirectResponse("/dashboard/login", status_code=302)
        # Clear BOTH scopes — the parent-domain cookie (one-door) and any legacy
        # host-only cookie from before the change.
        resp.delete_cookie(SESSION_COOKIE, path="/", domain=self._cookie_domain())
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
            return HTMLResponse(await self._render_dashboard(session, error="No such user."), status_code=400)
        try:
            c = self.registry.social.request_contact(me.id, other.id)
            kind = "contact_accepted" if c.status == "accepted" else "contact_request"
            self.registry.social.create_notification(other.id, kind, f"@{me.handle} wants to connect")
            return HTMLResponse(await self._render_dashboard(session, notice=f"Request sent to @{other.handle}."))
        except SocialError as exc:
            return HTMLResponse(await self._render_dashboard(session, error=str(exc)), status_code=400)

    async def accept_contact(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        other = self.registry.tenancy.user_by_handle(str(form.get("handle", "")).strip().lstrip("@"))
        if me is None or other is None:
            return HTMLResponse(await self._render_dashboard(session, error="No such user."), status_code=400)
        try:
            self.registry.social.accept_contact(me.id, other.id)
            self.registry.social.create_notification(other.id, "contact_accepted", f"@{me.handle} accepted your request")
            return HTMLResponse(await self._render_dashboard(session, notice=f"You're now connected with @{other.handle}."))
        except SocialError as exc:
            return HTMLResponse(await self._render_dashboard(session, error=str(exc)), status_code=400)

    async def read_notifications(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        if me is not None:
            self.registry.social.mark_notifications_read(me.id)
        return HTMLResponse(await self._render_dashboard(session, notice="Notifications cleared."))

    async def save_profile(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        handle = self._account_handle(session)
        if handle is None:
            return PlainTextResponse("No account.", status_code=403)
        form = await request.form()
        avatar_url = str(form.get("avatar_url", "")).strip()
        if avatar_url and (
            len(avatar_url) > 100_000
            or not (avatar_url.startswith("data:image/") or avatar_url.startswith("https://"))
        ):
            return HTMLResponse(
                await self._render_dashboard(
                    session, error="Avatar must be an https:// link or an uploaded image, under 100KB."
                ),
                status_code=400,
            )
        try:
            self.registry.tenancy.set_profile(
                handle,
                display_name=str(form.get("display_name", "")),
                avatar_url=avatar_url,
                bio=str(form.get("bio", "")),
            )
            return HTMLResponse(await self._render_dashboard(session, notice="Profile saved."))
        except TenancyError as exc:
            # Covers tenancy's own https://-only backstop — e.g. a data:image/ avatar,
            # which our client-side canvas can produce, until that store-level rule
            # is relaxed to admit data URIs too.
            return HTMLResponse(await self._render_dashboard(session, error=str(exc)), status_code=400)

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

    async def api_asks(self, request: "Request") -> "Response":
        """What is blocked on Hiren right now, across rooms AND threads.

        `ask_human` reaches him well; ANSWERING it has meant opening a page he
        rarely opens. This is the read half of closing that loop from the tray —
        the one surface that is alive when no session is."""
        from starlette.responses import JSONResponse

        user = self._bearer_user(request) or self._session_user(self._session(request) or {})
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        try:
            rows = self.registry.rooms.awaiting_human(user.id)
        except Exception:  # noqa: BLE001
            log.warning("could not list pending questions", exc_info=True)
            rows = []
        out = []
        for r in rows:
            name = str(r["name"])
            is_thread = name.startswith(_THREAD_FLOOR_PREFIX)
            out.append({
                "name": name[len(_THREAD_FLOOR_PREFIX):] if is_thread else name,
                "kind": "thread" if is_thread else "room",
                "question": r["question"],
                "since": r["since"],
            })
        return JSONResponse({"ok": True, "asks": out})

    async def api_answer_ask(self, request: "Request") -> "Response":
        """Answer a blocked room or thread as the signed-in PERSON.

        Mirrors kb_room_relay_answer, which does the same job from inside a
        session. Both exist because he is reachable in two different places and
        should be able to answer from whichever one he is actually in."""
        from starlette.responses import JSONResponse

        user = self._bearer_user(request) or self._session_user(self._session(request) or {})
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        name = str(payload.get("name") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        if not name or not answer:
            return JSONResponse(
                {"ok": False, "error": "name and answer are both required"},
                status_code=400,
            )
        try:
            room = self.registry.rooms.room_by_name(name.lower())
            if room is not None:
                # session="app" marks it as the PERSON, which is what clears the
                # block and hands the floor back to whoever asked.
                self.registry.rooms.post_turn(room.id, user.id, answer, session="app")
                return JSONResponse({"ok": True, "kind": "room", "name": name})
            # Not a room — a thread, whose floor lives in a shadow room and whose
            # transcript lives in git, so nothing passes through post_turn to
            # notice the answer. Hence the explicit unblock.
            shadow = self.registry.rooms.room_by_name(
                f"{_THREAD_FLOOR_PREFIX}{name.lower()}"
            )
            if shadow is None:
                return JSONResponse(
                    {"ok": False, "error": f"no open room or thread named '{name}'"},
                    status_code=404,
                )
            store = await self.registry.store_for_handle(user.handle)
            await store.kb_thread_post(name, user.handle, answer, False, "", "", "",
                                       None, False, False, 0)
            self.registry.rooms.answer_human(shadow.id)
            return JSONResponse({"ok": True, "kind": "thread", "name": name})
        except Exception as exc:  # noqa: BLE001
            log.warning("could not answer %r", name, exc_info=True)
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)

    async def api_presence_upload(self, request: "Request") -> "Response":
        """Accept presence records from a machine that is NOT the server's.

        Auto-presence has been single-machine by construction: the Claude Code hook
        writes a spool file locally, and only the server drains it — from the
        server's own disk. So Hiren's Mac has never appeared in the roster, and its
        records were not queued but stranded. This is the missing limb: the tray
        runs on that machine, already holds an OAuth token, and can post what the
        hook wrote.

        Records go through the SAME batched write path the local ingest uses, so
        there is still exactly one git writer and one commit per batch. Nothing
        here bypasses the lock.
        """
        from starlette.responses import JSONResponse

        user = self._bearer_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        raw = payload.get("records")
        if not isinstance(raw, list) or not raw:
            return JSONResponse(
                {"ok": False, "error": "records must be a non-empty list"}, status_code=400
            )
        # A machine that has been offline can hold a lot; cap so one upload cannot
        # turn into an unbounded commit.
        records = [r for r in raw if isinstance(r, dict) and str(r.get("session") or "").strip()]
        if not records:
            return JSONResponse({"ok": False, "error": "no valid records"}, status_code=400)
        records = records[:50]

        try:
            store = await self.registry.store_for_handle(user.handle)
            written: list[str] = []
            await store._locked_commit(  # noqa: SLF001 — same path the local ingest uses
                lambda: store._presence_write_batch(  # noqa: SLF001
                    records, self.settings.presence_refresh_minutes, written
                ),
                f"workspace: presence upload ({len(records)} from a remote machine)",
            )
            return JSONResponse({"ok": True, "ingested": len(written),
                                 "received": len(records)})
        except Exception as exc:  # noqa: BLE001 — presence must never 500 a client
            log.warning("presence upload failed", exc_info=True)
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)

    async def api_notifications(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        from engram_server import pushbus

        user = self._bearer_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        social = self.registry.social

        # LIVE delivery (?wait=N&since=<max seen id>): park the request on the
        # user's wake bus until a notification NEWER than the client's cursor
        # exists (or timeout) — a standing unread backlog therefore parks too,
        # instead of tight-looping the client at one request per second.
        try:
            wait_s = float(request.query_params.get("wait", 0) or 0)
            since = int(request.query_params.get("since", 0) or 0)
        except ValueError:
            wait_s, since = 0.0, 0
        notes = social.list_notifications(user.id, unread_only=True)
        if wait_s > 0 and not any(n.id > since for n in notes):
            await pushbus.wait(user.id, wait_s)
            notes = social.list_notifications(user.id, unread_only=True)
        counts = social.unread_counts(user.id)
        return JSONResponse({
            "ok": True,
            "unread": [{"id": n.id, "kind": n.kind, "body": n.body, "at": n.created, "ref": n.ref} for n in notes],
            "counts": counts,
        })

    async def api_mark_read(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        user = self._bearer_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        # Optional JSON body {"ids": [..]} marks just those (acting on ONE
        # notification — e.g. clicking its Open button — clears that one, not
        # everything). No/empty body keeps the mark-ALL behavior.
        ids: list[int] | None = None
        try:
            payload = await request.json()
            raw = payload.get("ids") if isinstance(payload, dict) else None
            if isinstance(raw, list):
                ids = [int(x) for x in raw]
        except Exception:  # noqa: BLE001 — no body / not json = mark all
            ids = None
        self.registry.social.mark_notifications_read(user.id, ids)
        return JSONResponse({"ok": True})

    def _cookie_domain(self) -> str | None:
        """ONE DOOR: the session must work on BOTH hostnames (engram. serves the OAuth
        callback, brain. serves the humans), so when public_url and explorer_url share
        a registrable parent the cookie is scoped to it (".metalfinger.xyz"). Host-only
        (None) otherwise — tests, localhost, single-host deployments. Naive about
        multi-part TLDs (co.uk) by design; both production hosts are *.metalfinger.xyz."""
        a = (urlsplit(self.settings.public_url).hostname or "").lower()
        b = (urlsplit(self.settings.explorer_url or "").hostname or "").lower()
        if not a or not b or a == b or "." not in a:
            return None
        pa, pb = a.split("."), b.split(".")
        if len(pa) >= 2 and len(pb) >= 2 and pa[-2:] == pb[-2:]:
            return "." + ".".join(pa[-2:])
        return None

    def _logged_in_redirect(self, subject: str, email_or_login: str, handle: str) -> "Response":
        # Send humans HOME — the callback lands on the machine hostname (engram.*),
        # but the human surface is the explorer hostname. An absolute redirect (the
        # parent-domain cookie travels with it) means you always end up on brain.*
        # after sign-in instead of stranded on the connector host.
        human_base = (self.settings.explorer_url or self.settings.public_url).rstrip("/")
        resp = RedirectResponse(f"{human_base}/dashboard", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE,
            self.auth.issue(subject, email_or_login, handle),
            max_age=self.settings.dashboard_session_ttl_hours * 3600,
            httponly=True, secure=True, samesite="lax", path="/",
            domain=self._cookie_domain(),
        )
        return resp

    # -- rendering -----------------------------------------------------------

    def _page(self, title: str, body: str) -> str:
        """The PRE-SIGN-IN shell (login / invite / claim-handle / errors).

        Same stylesheet and palette as the signed-in pages so the very first thing an
        invited person sees already looks like Engram — just centred, with no topbar or
        sidebar, since there's no session (and so no projects) to navigate yet."""
        from engram_server.explorer.html import CSS

        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_html.escape(title)} — Engram</title>"
            f"<style>{CSS}"
            # Centred single-column layout for the auth pages only.
            ".authwrap{max-width:34rem;margin:0 auto;padding:3rem 1.25rem}"
            ".authwrap .wordmark{display:inline-flex;margin:0 0 1.5rem}"
            ".authwrap .card{margin:.9rem 0}"
            ".notice{background:var(--surface-2);border:1px solid var(--line-2);"
            "color:var(--fg);padding:.6rem .8rem;border-radius:8px}"
            ".error{background:var(--amber-bg);border:1px solid var(--amber-bd);"
            "color:var(--amber-tx);padding:.6rem .8rem;border-radius:8px}"
            "</style></head><body><div class=authwrap>"
            "<a class=wordmark href='/'><span class=mark>◗</span>Engram</a>"
            f"<div class='page-head'><div><h1>{_html.escape(title)}</h1></div></div>"
            f"{body}</div></body></html>"
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
        if next_kind == "ext":
            lead, title = "<p>Sign in to connect the notifier extension.</p>", "Sign in"
        elif next_kind == "signup":
            title = "Create your Engram"
            lead = ("<p>Your AI's long-term memory — a private knowledge base your Claude or "
                    "ChatGPT reads and writes across every session and device. Free, and yours: "
                    "it's a git repo you can take with you.</p>")
        elif next_kind == "join":
            lead, title = "<p>You've been invited. Sign in to set up your Engram.</p>", "Create your Engram"
        else:
            lead, title = "", "Sign in"
        footer = ""
        if next_kind == "login" and self.settings.open_signup:
            footer = "<p class='meta'>New here? <a href='/join'>Create your Engram →</a></p>"
        elif next_kind == "signup":
            footer = "<p class='meta'>Already have an account? <a href='/dashboard/login'>Sign in</a></p>"
        return self._page(
            title,
            f"{lead}<div class=card>{buttons or '<p>No sign-in method configured.</p>'}</div>{footer}",
        )

    def _render_claim(self, suggested: str, email: str, error: str | None = None,
                      signup: bool = False) -> str:
        # Suggest a handle from the login/email local-part, sanitized.
        seed = suggested.split("@", 1)[0].lower()
        safe = "".join(c for c in seed if c.isalnum() or c == "-").strip("-")[:32] or "me"
        err = f"<p class=error>{_html.escape(error)}</p>" if error else ""
        lead = (
            f"<p>Signed in as <b>{_html.escape(email)}</b>. Pick the handle you'll be known by.</p>"
            if signup else
            f"<p>You're accepting an invite for <b>{_html.escape(email)}</b>.</p>"
        )
        return self._page(
            "Choose your handle",
            f"{lead}{err}"
            "<form method=post action='/join/claim'><div class=card>"
            "<p>Your handle is how people find and message you — lowercase letters, digits "
            "and hyphens.</p>"
            f"<p>@ <input name=handle value='{_html.escape(safe)}' "
            "pattern='[a-z0-9-]{2,32}' required></p>"
            "<button type=submit>Create my Engram</button>"
            "<p class='meta'>This creates your own private knowledge base. Nothing you write "
            "is visible to anyone until you publish it.</p></div></form>",
        )

    async def _render_dashboard(self, session: dict, notice: str | None = None,
                                error: str | None = None, projects: list | None = None) -> str:
        """The home. Rendered in the SAME rich shell as every other page (topbar,
        projects sidebar, explorer CSS) — the home used to carry its own minimal
        stylesheet, which made the first page you land on look unlike the rest."""
        handle = self._account_handle(session) or "?"
        user = self.registry.tenancy.user_by_handle(handle)
        if projects is None:
            projects = await self._user_projects(session)
        banner = (f"<p class=notice>{_html.escape(notice)}</p>" if notice else "") + (
            f"<p class=error>{_html.escape(error)}</p>" if error else ""
        )
        name = (user.display_name if user else None) or f"@{handle}"
        bio = (user.bio if user else None) or ""
        hero = (
            "<div class='page-head'>"
            f"{self._avatar_img(user, 56)}"
            f"<div><p class='eyebrow'>Your Engram</p><h1>{_html.escape(str(name))}</h1>"
            f"<p class='meta'>@{_html.escape(handle)}"
            + (f" · {_html.escape(bio)}" if bio else "")
            + "</p></div></div>"
        )
        body = [banner, hero]
        body.append("<p class='section-label'>Projects</p>")
        body.append(self._render_projects_block(projects))
        body.append("<p class='section-label'>Activity</p>")
        body.append(self._render_social_block(session))
        body.append("<p class='section-label'>Your account</p>")
        body.append(self._render_profile_block(session, handle))
        body.append(self._connect_block(handle))
        body.append(self._render_extension_block(session, handle))
        if self._is_owner(session):
            body.append("<p class='section-label'>Admin</p>")
            body.append(self._render_admin())
        body.append("<div class='card'><form method=post action='/dashboard/logout'>"
                    "<button type=submit>Sign out</button></form></div>")
        return self._brain_shell(
            f"@{handle}", "".join(body), [("home", "/dashboard")],
            self._brain_sidebar(projects), active_tab="home",
        )

    def _avatar_img(self, user, size: int = 40) -> str:
        """A safe <img> for a user's avatar, or a deterministic-color initials fallback.
        Thin wrapper over ``_avatar_for`` for call sites that already hold a User row."""
        if user is None:
            return self._avatar_for("", "", "", size)
        return self._avatar_for(user.handle, user.display_name or "", user.avatar_url or "", size)

    # Deterministic per-handle color so the same person always gets the same
    # initials-circle color across people/profile/rooms/office — a stable visual
    # identity even before someone sets a real avatar.
    _AVATAR_COLORS = (
        "#b5622b", "#2f6f4e", "#3a5a9c", "#8a4b8a",
        "#a3762b", "#3f7a8a", "#a34b4b", "#5a6b3a",
    )

    @classmethod
    def _color_for_handle(cls, handle: str) -> str:
        h = sum(ord(c) for c in handle) if handle else 0
        return cls._AVATAR_COLORS[h % len(cls._AVATAR_COLORS)]

    def _avatar_for(self, handle: str, display_name: str = "", avatar_url: str = "", size: int = 40) -> str:
        """A safe <img> for an https:// or data:image/ avatar, or an initials-circle
        fallback colored deterministically from the handle."""
        if avatar_url:
            return (
                f"<img src='{_html.escape(avatar_url, quote=True)}' "
                f"width={size} height={size} alt='' "
                "style='border-radius:50%;object-fit:cover;vertical-align:middle'>"
            )
        letter = _html.escape(((display_name or handle or "?").strip()[:1] or "?").upper())
        color = self._color_for_handle(handle)
        return (
            f"<span style='display:inline-flex;width:{size}px;height:{size}px;border-radius:50%;"
            f"background:{color};color:#fff;align-items:center;justify-content:center;"
            f"font-weight:600;vertical-align:middle'>{letter}</span>"
        )

    def _render_profile_block(self, session: dict, handle: str) -> str:
        user = self.registry.tenancy.user_by_handle(handle)
        name = (user.display_name if user else "") or ""
        avatar = (user.avatar_url if user else "") or ""
        bio = (user.bio if user else "") or ""
        return (
            "<div class=card><h2>Your profile</h2>"
            f"<p>{self._avatar_img(user)} <span class=mono>@{_html.escape(handle)}</span></p>"
            "<form method=post action='/dashboard/profile' id=engram-profile-form>"
            "<p>Display name<br><input name=display_name maxlength=60 "
            f"value='{_html.escape(name)}' placeholder='e.g. Amiyanshu'></p>"
            "<p>Bio<br><textarea name=bio maxlength=280 placeholder='A line about you'>"
            f"{_html.escape(bio)}</textarea></p>"
            "<p>Avatar<br><input type=file id=engram-avatar-file accept='image/*'> "
            "<span style='font-size:12px;color:var(--muted)'>or paste an https:// image URL below</span><br>"
            "<input name=avatar_url id=engram-avatar-url style='width:100%' "
            f"value='{_html.escape(avatar)}' placeholder='https://…/me.png'></p>"
            "<button type=submit>Save profile</button></form>"
            # Client-side downscale: a chosen file becomes a 96x96 JPEG data-URL in the
            # same field a pasted https:// URL would use — the server treats both the
            # same way (see save_profile's avatar_url validation).
            "<script>(function(){"
            "var f=document.getElementById('engram-avatar-file');"
            "if(!f)return;"
            "f.addEventListener('change',function(e){"
            "var file=e.target.files&&e.target.files[0];if(!file)return;"
            "var reader=new FileReader();"
            "reader.onload=function(ev){"
            "var img=new Image();"
            "img.onload=function(){"
            "var c=document.createElement('canvas');c.width=96;c.height=96;"
            "var ctx=c.getContext('2d');"
            "var s=Math.min(img.width,img.height);"
            "var sx=(img.width-s)/2,sy=(img.height-s)/2;"
            "ctx.drawImage(img,sx,sy,s,s,0,0,96,96);"
            "document.getElementById('engram-avatar-url').value=c.toDataURL('image/jpeg',0.85);"
            "};img.src=ev.target.result;};"
            "reader.readAsDataURL(file);"
            "});"
            "})();</script>"
            "</div>"
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

    # -- brain rendering: the per-user home reuses the explorer's rich CSS +
    #    content helpers, pointed at THIS user's brain with /dashboard/ links.
    #    (The operator explorer at /brain/* is untouched.)

    @staticmethod
    def _dash_links(html: str) -> str:
        """Repoint explorer-built internal links (/brain/f/, /brain/p/) at this home."""
        return html.replace('="/brain/f/', '="/dashboard/f/').replace('="/brain/p/', '="/dashboard/p/')

    # The five-tab product IA (v3). Keys are what callers pass as ``active_tab``.
    _NAV_TABS = (
        ("home", "/dashboard", "Home"),
        ("browse", "/dashboard/search", "Browse"),
        ("people", "/dashboard/people", "People"),
        ("rooms", "/dashboard/rooms", "Rooms"),
        ("office", "/dashboard/office", "Office"),
    )
    # Polish pass (Hiren, 2026-08-01): a product has FIVE destinations and an
    # account menu — not eleven links. The old secondary row folds into the tabs
    # as sub-tabs (Browse: Search|Artifacts|Graph|Activity · People: Directory|
    # Feed|Questions) and the person-scoped pages live under the avatar menu.
    _NAV_MORE = ()
    _BROWSE_SUBTABS = (
        ("/dashboard/search", "Search"),
        ("/dashboard/artifacts", "Artifacts"),
        ("/dashboard/graph", "Graph"),
        ("/dashboard/activity", "Activity"),
    )
    _PEOPLE_SUBTABS = (
        ("/dashboard/people", "Directory"),
        ("/dashboard/feed", "Feed"),
        ("/dashboard/asks", "Questions"),
    )

    def _subtabs(self, tabs, current_path: str) -> str:
        from engram_server.explorer.html import esc

        return (
            "<div class='subtabs'>"
            + "".join(
                f"<a href='{href}' class='{'on' if href == current_path else ''}'>{esc(label)}</a>"
                for href, label in tabs
            )
            + "</div>"
        )

    def _brain_shell(self, title: str, body: str, crumbs, sidebar_html: str, active_tab: str = "",
                     is_owner: bool = False, handle: str = "") -> str:
        from engram_server.explorer.html import CSS, esc

        crumb_html = ""
        if crumbs:
            links = [f'<a href="{esc(h)}">{esc(l)}</a>' for l, h in crumbs]
            crumb_html = '<nav class="crumbs">' + '<span class="sep">/</span>'.join(links) + "</nav>"
        primary = "".join(
            f"<a href='{href}'" + (" style='color:var(--accent-ink);font-weight:700'" if key == active_tab else "")
            + f">{esc(label)}</a>"
            for key, href, label in self._NAV_TABS
        )
        # Account menu: person-scoped destinations live behind the avatar, not in
        # the nav. Pure CSS (details/summary) — no JS to break.
        menu_links = "<a href='/dashboard'>Profile &amp; account</a><a href='/dashboard/setup'>Setup</a>"
        if is_owner:
            menu_links += "<a href='/dashboard/ops'>Ops</a>"
        account = (
            "<details class='acct'><summary>"
            + (self._avatar_for(handle or "you", size=26) if handle else "☰")
            + "</summary><div class='acct-menu'>" + menu_links
            + "<form method=post action='/dashboard/logout'><button type=submit>Sign out</button></form>"
            + "</div></details>"
        )
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>{esc(title)} — Engram</title><style>{CSS}"
            # Visibility badges: public is loud on purpose, private is quiet but present.
            ".badge.vis-public{background:var(--accent);color:var(--accent-fg);font-weight:600}"
            ".badge.vis-contacts{background:var(--accent-soft);color:var(--accent-ink)}"
            ".badge.vis-private{background:var(--surface-2);color:var(--muted)}"
            # --- v3 form layer: the explorer never had forms, so inputs/buttons fell
            # back to raw browser defaults (white boxes on a dark theme). Everything
            # interactive gets the house look here.
            ".layout input[type=text],.layout input[type=search],.layout input:not([type])"
            ",.layout textarea,.layout select{background:var(--inset);color:var(--fg);"
            "border:1px solid var(--line-2);border-radius:8px;padding:.55rem .7rem;"
            "font:inherit;width:100%;box-sizing:border-box;margin:.15rem 0 .2rem}"
            ".layout input:focus,.layout textarea:focus{outline:2px solid var(--accent);"
            "outline-offset:1px;border-color:var(--accent-line)}"
            ".layout textarea{min-height:4.5rem;resize:vertical}"
            ".layout button,.layout input[type=submit]{background:var(--accent);"
            "color:var(--accent-fg);border:0;border-radius:8px;padding:.55rem 1.1rem;"
            "font:inherit;font-weight:600;cursor:pointer}"
            ".layout button:hover{filter:brightness(1.08)}"
            ".layout form p{margin:.5rem 0;color:var(--muted);font-size:.85rem}"
            ".notice{background:var(--accent-soft);color:var(--accent-ink);"
            "border:1px solid var(--accent-line);border-radius:8px;padding:.6rem .9rem}"
            ".error{background:color-mix(in srgb,var(--red) 14%,transparent);color:var(--red);"
            "border:1px solid var(--red);border-radius:8px;padding:.6rem .9rem}"
            ".badge.unread{background:var(--accent);color:var(--accent-fg);border-radius:999px;"
            "padding:.05rem .5rem;font-size:.72rem;font-weight:700;vertical-align:middle}"
            # rooms: chat column + composer + list cards
            ".thread-transcript{background:var(--surface);border:1px solid var(--line);"
            "border-radius:12px;padding:1rem;display:flex;flex-direction:column;gap:.55rem;"
            "margin:.4rem 0 .8rem}"
            ".bubble{max-width:78%}.bubble.right{align-self:flex-end}.bubble.left{align-self:flex-start}"
            ".composer{display:flex;gap:.5rem;align-items:flex-end}"
            ".composer textarea{flex:1 1 auto;min-height:2.8rem;margin:0}"
            ".composer button{flex:0 0 auto}"
            ".closeform{display:flex;gap:.5rem;align-items:center;margin-top:.6rem}"
            ".closeform input{flex:1 1 auto;margin:0}"
            ".closeform button{flex:0 0 auto;background:var(--surface-2);color:var(--fg);"
            "border:1px solid var(--line-2)}"
            ".newroom{max-width:34rem}"
            ".bigsearch{display:flex;gap:.5rem;max-width:38rem;margin:.4rem 0 1rem}"
            ".bigsearch input{flex:1 1 auto;margin:0;font-size:1rem;padding:.7rem .9rem}"
            ".bigsearch button{flex:0 0 auto}"
            ".stat-row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:.3rem 0}"
            # sub-tabs (Browse: Search|Artifacts|Graph|Activity · People: Directory|Feed|Questions)
            ".subtabs{display:flex;gap:.2rem;border-bottom:1px solid var(--line);margin:0 0 1rem}"
            ".subtabs a{padding:.45rem .8rem;color:var(--muted);font-size:.85rem;border-bottom:2px solid transparent}"
            ".subtabs a:hover{color:var(--fg);text-decoration:none}"
            ".subtabs a.on{color:var(--accent-ink);border-bottom-color:var(--accent);font-weight:600}"
            # account menu (avatar dropdown, pure CSS)
            ".acct{position:relative;margin-left:.7rem}"
            ".acct summary{list-style:none;cursor:pointer;display:flex;align-items:center}"
            ".acct summary::-webkit-details-marker{display:none}"
            ".acct-menu{position:absolute;right:0;top:2.2rem;background:var(--surface);"
            "border:1px solid var(--line-2);border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.35);"
            "padding:.4rem;min-width:11rem;z-index:50;display:flex;flex-direction:column}"
            ".acct-menu a,.acct-menu button{display:block;width:100%;text-align:left;padding:.5rem .7rem;"
            "border-radius:7px;color:var(--fg);background:none;border:0;font:inherit;font-size:.85rem;cursor:pointer}"
            ".acct-menu a:hover,.acct-menu button:hover{background:var(--surface-2);text-decoration:none}"
            # empty states as invitations, not shrugs
            ".empty-cta{background:var(--surface);border:1px dashed var(--line-2);border-radius:12px;"
            "padding:1.2rem;text-align:center;color:var(--muted);margin:.6rem 0}"
            ".empty-cta b{color:var(--fg)}"
            ".via-chip{font-size:.62rem;background:var(--surface-2);border:1px solid var(--line-2);"
            "border-radius:999px;padding:.02rem .45rem;color:var(--muted);margin-left:.35rem}"
            "</style></head><body>"
            "<input type=checkbox id=navcb class=navcb aria-hidden=true>"
            "<header class=topbar><div class=topbar-inner>"
            "<label for=navcb class=burger aria-label='Toggle navigation'>≡</label>"
            "<a class=wordmark href='/dashboard'><span class=mark>◗</span>Engram</a>"
            "<form class=search role=search action='/dashboard/search' method=get>"
            "<input type=search name=q placeholder='Search your brain…' aria-label='Search' autocomplete=off></form>"
            # The five-tab IA is the product surface; older deep links (Feed, Questions,
            # Graph, Activity, ...) still work — they're a quiet second row, not gone.
            f"<nav class=topnav>{primary}</nav>{account}"
            "</div></header><div class=layout>"
            f"<aside class=sidebar aria-label='Bundle navigation'>{sidebar_html}</aside>"
            f"<main>{crumb_html}{body}</main></div></body></html>"
        )

    def _brain_sidebar(self, projects: list, active: str | None = None) -> str:
        from engram_server.explorer.html import esc

        # No "Home" entry — the wordmark in the topbar is the way home.
        items: list[str] = []
        live = [p for p in projects if str(p.get("status") or "active") != "archived"]
        folders: dict[str, list] = {}
        for p in live:
            folders.setdefault(str(p.get("folder") or ""), []).append(p)

        def _links(bucket: list) -> str:
            return "".join(
                f"<a class='{'nav-link active' if str(p['id']) == active else 'nav-link'}' "
                f"href='/dashboard/p/{esc(str(p['id']))}'>{esc(str(p.get('title') or p['id']))}</a>"
                for p in bucket
            )

        for name in sorted(k for k in folders if k):
            items.append(f"<p class='nav-group'>📁 {esc(name)}</p>")
            items.append(_links(folders[name]))
        if folders.get(""):
            items.append("<p class='nav-group'>Projects</p>")
            items.append(_links(folders[""]))
        return "<nav class='nav-sub'>" + "".join(items) + "</nav>"

    @staticmethod
    def _project_default_visibility(store, path: str) -> str:
        """The visibility a concept inherits from its project's context.md."""
        from engram_server.explorer.render import split_frontmatter

        parts = [p for p in path.split("/") if p]
        root = f"projects/{parts[1]}" if parts[:1] == ["projects"] and len(parts) > 1 else (parts[0] if parts else "")
        ctx = store.root / root / "context.md"
        if not ctx.is_file():
            return "private"
        try:
            meta, _b = split_frontmatter(ctx.read_text(encoding="utf-8"))
        except OSError:
            return "private"
        vis = str(meta.get("visibility") or "").strip().lower()
        return vis if vis in ("public", "contacts", "private") else "private"

    @staticmethod
    def _vis_badge(visibility: str, *, always: bool = True) -> str:
        """A badge saying who can see this. Rendered for EVERY state (including private)
        so exposure is never inferred from the absence of a mark — silently-public work
        is the failure mode this model exists to prevent."""
        from engram_server.explorer.html import esc

        vis = (visibility or "private").lower()
        label = {"public": "🌐 public", "contacts": "👥 contacts", "private": "🔒 private"}.get(
            vis, "🔒 private"
        )
        if vis == "private" and not always:
            return ""
        return f"<span class='badge vis-{esc(vis)}'>{esc(label)}</span>"

    @staticmethod
    def _project_card(p: dict) -> str:
        from engram_server.explorer.html import chip, esc

        tags = "".join(chip(t) for t in (p.get("tags") or []))
        vis = Dashboard._vis_badge(str(p.get("visibility") or "private"))
        return (
            f"<a class='card' href='/dashboard/p/{esc(str(p['id']))}'>"
            f"<h3>{esc(str(p.get('title') or p['id']))}</h3>"
            + (f"<p class='desc'>{esc(str(p['description']))}</p>" if p.get("description") else "")
            + f"<div class='stat-row'>{vis}{tags}</div>"
            + (f"<p class='meta'>{esc(str(p['last_session']))}</p>" if p.get("last_session") else "")
            + "</a>"
        )

    def _render_projects_block(self, projects: list) -> str:
        """Projects grouped by their context.md `tags` (archived ones tucked away last)."""
        from engram_server.explorer.html import esc

        if not projects:
            return ("<div class='card'><p class='empty'>No projects yet — start one from your "
                    "Claude (say \"start a project for X\"), and it'll show here.</p></div>")

        live = [p for p in projects if str(p.get("status") or "active") != "archived"]
        archived = [p for p in projects if str(p.get("status") or "active") == "archived"]

        # Group by the real folder each project lives in; "" = top level.
        folders: dict[str, list] = {}
        for p in live:
            folders.setdefault(str(p.get("folder") or ""), []).append(p)

        parts: list[str] = []
        for name in sorted(k for k in folders if k):
            parts.append(f"<p class='section-label'>📁 {esc(name)}</p>")
            parts.append("<div class='cards'>" + "".join(self._project_card(p) for p in folders[name]) + "</div>")
        if folders.get(""):
            if parts:  # only label the loose ones when folders exist above them
                parts.append("<p class='section-label'>Not in a folder</p>")
            parts.append("<div class='cards'>"
                         + "".join(self._project_card(p) for p in folders[""]) + "</div>")
        if archived:
            parts.append(
                f"<details><summary class='meta'>Archived ({len(archived)})</summary>"
                "<div class='cards'>" + "".join(self._project_card(p) for p in archived) + "</div></details>"
            )
        return "".join(parts)

    def _render_project(self, store, project: str, rel: str, projects: list) -> str:
        from engram_server.explorer.html import esc
        from engram_server.explorer.render import render_markdown, split_frontmatter
        from engram_server.explorer.routes import (
            _log_entries, _project_section_render, _timeline,
        )

        pdir = store.root / rel
        meta, body_md = ({}, "")
        ctx = pdir / "context.md"
        if ctx.is_file():
            try:
                meta, body_md = split_frontmatter(ctx.read_text(encoding="utf-8"))
            except OSError:
                pass
        title = str(meta.get("title") or project)
        vis = str(meta.get("visibility") or "private").lower()
        parts = [
            f"<div class='page-head'><div><p class='eyebrow'>Project</p><h1>{esc(title)}</h1></div></div>",
            f"<div class='stat-row'>{self._vis_badge(vis)}"
            + "".join(f"<span class='chip'>{esc(t)}</span>" for t in _tags_of(meta.get("tags")))
            + "</div>",
        ]
        if vis == "public":
            parts.append("<p class='meta'>Everything in this project is visible to other "
                         "Engram users unless a concept says otherwise.</p>")
        if body_md:
            parts.append(f"<div class='md'>{self._dash_links(render_markdown(body_md, f'{rel}/context.md'))}</div>")
        entries = _log_entries(pdir / "log.md")
        parts.append("<p class='section-label'>Recent sessions</p>")
        parts.append(self._dash_links(_timeline(entries, f"{rel}/log.md")) if entries
                     else "<p class='empty'>No sessions logged yet.</p>")
        try:
            _stat, section_html, _browse, _demoted = _project_section_render(pdir, store.root)
            for sec in section_html:
                parts.append(self._dash_links(sec))
        except Exception:  # noqa: BLE001 — section rendering is best-effort
            pass
        # Folder filing (folders are audiences): move this project into any folder —
        # existing ones offered via datalist, a new name creates the folder.
        current_folder = rel.split("/")[1] if rel.startswith("projects/") and rel.count("/") == 2 else ""
        folder_opts = "".join(
            f"<option value='{esc(f)}'>"
            for f in sorted({str(p.get('folder') or '') for p in projects} - {""})
        )
        parts.append(
            "<p class='section-label'>Folder</p>"
            f"<form class='closeform' method='post' action='/dashboard/p/{esc(project)}/move'>"
            f"<input name='folder' list='folders' value='{esc(current_folder)}' "
            "placeholder='e.g. personal — empty = no folder'>"
            f"<datalist id='folders'>{folder_opts}</datalist>"
            "<button type='submit'>Move</button></form>"
        )
        crumbs = [("home", "/dashboard"), (project, f"/dashboard/p/{project}")]
        return self._brain_shell(title, "\n".join(parts), crumbs, self._brain_sidebar(projects, active=project),
                                 active_tab="home")

    @staticmethod
    def _graph_dashify(html: str) -> str:
        """Repoint the standalone graph page at the per-user home. The node-click JS
        uses a '/brain/f/' literal; the operator-only topnav items are neutered to Home."""
        html = html.replace("'/brain/f/'", "'/dashboard/f/'").replace('"/brain/f/', '"/dashboard/f/')
        html = html.replace('href="/brain/graph"', 'href="/dashboard/graph"')
        html = html.replace('href="/brain/activity"', 'href="/dashboard/activity"')
        for op in ("/brain/office", "/brain/workspace", "/brain/threads", "/brain/system"):
            html = html.replace(f'href="{op}"', 'href="/dashboard"')
        return html.replace('href="/brain"', 'href="/dashboard"')

    def _render_search(self, q: str, results: list, projects: list) -> str:
        from engram_server.explorer.html import esc

        parts = [f"<div class='page-head'><div><p class='eyebrow'>Browse</p>"
                 f"<h1>{esc(q) if q else 'Search your brain'}</h1></div></div>",
                 self._subtabs(self._BROWSE_SUBTABS, '/dashboard/search')]
        # The query box lives ON the page (the topbar box alone confused real users —
        # 'no place to type the query'). Autofocus when empty, keep the value when not.
        parts.append(
            "<form method='get' action='/dashboard/search' class='bigsearch'>"
            f"<input type='search' name='q' value='{esc(q)}' placeholder='Search your brain…' "
            + ("autofocus " if not q else "")
            + "aria-label='Search query'><button type='submit'>Search</button></form>"
        )
        if not q:
            parts.append("<p class='empty'>Hybrid semantic + text search across every concept in your brain.</p>")
        elif not results:
            parts.append("<p class='empty'>No matches.</p>")
        else:
            cards = []
            for h in results:
                path = str(h.get("path", "")) if isinstance(h, dict) else str(h)
                title = str((h.get("title") if isinstance(h, dict) else None) or path.rsplit("/", 1)[-1])
                snip = str(h.get("snippet") or h.get("description") or "") if isinstance(h, dict) else ""
                cards.append(
                    f"<a class='card' href='/dashboard/f/{esc(path)}'><h3>{esc(title)}</h3>"
                    + (f"<p class='desc'>{esc(snip[:180])}</p>" if snip else "")
                    + f"<p class='meta'>{esc(path)}</p></a>"
                )
            parts.append(f"<div class='cards'>{''.join(cards)}</div>")
        crumbs = [("home", "/dashboard"), ("search", "/dashboard/search")]
        return self._brain_shell("Search", "".join(parts), crumbs, self._brain_sidebar(projects), active_tab="browse")

    async def _render_activity(self, store, projects: list) -> str:
        from anyio import to_thread as _tt

        from engram_server.explorer.format import humanize_time
        from engram_server.explorer.html import badge, esc
        from engram_server.explorer.routes import _git_log

        parts = ["<div class='page-head'><div><p class='eyebrow'>Activity</p><h1>Recent changes</h1></div></div>"]
        try:
            rows = await _tt.run_sync(_git_log, store.root, self.settings.git_timeout)
        except Exception as exc:  # noqa: BLE001
            rows = []
            parts.append(f"<p class='empty'>History unavailable: {esc(exc)}</p>")
        if rows:
            items = []
            for sha, date, author, subject in rows:
                rel, exact = humanize_time(date)
                items.append(
                    "<div class='tl-item'>"
                    f"<span class='tl-date' title='{esc(exact)}'>{esc(rel)}</span>"
                    f"<h3>{esc(subject)}</h3>"
                    f"<div class='tl-body meta'><code>{esc(sha)}</code> · {badge(author, 'accent')}</div></div>"
                )
            parts.append("<div class='timeline'>" + "".join(items) + "</div>")
        crumbs = [("home", "/dashboard"), ("activity", "/dashboard/activity")]
        return self._brain_shell(
            "Activity",
            self._subtabs(self._BROWSE_SUBTABS, "/dashboard/activity") + "".join(parts),
            crumbs, self._brain_sidebar(projects), active_tab="browse")

    def _render_concept(self, store, path: str, concept: dict, projects: list) -> str:
        from engram_server.explorer.html import esc
        from engram_server.explorer.render import render_markdown, split_frontmatter

        meta, body_md = ({}, concept.get("content") or "")
        try:
            meta, body_md = split_frontmatter(concept.get("content") or "")
        except Exception:  # noqa: BLE001
            pass
        title = str(meta.get("title") or path.rsplit("/", 1)[-1])
        segs = path.split("/")
        crumbs = [("home", "/dashboard")]
        proj = segs[1] if segs[0] == "projects" and len(segs) > 1 else segs[0]
        if proj:
            crumbs.append((proj, f"/dashboard/p/{proj}"))
        head = (f"<div class='page-head'><div>"
                f"<p class='eyebrow'>{esc(str(meta.get('type') or 'concept'))}</p><h1>{esc(title)}</h1></div></div>")
        # Effective reach: this concept's own visibility, else its project's default.
        own = str(meta.get("visibility") or "").strip().lower()
        eff = own or self._project_default_visibility(store, path)
        head += f"<div class='stat-row'>{self._vis_badge(eff)}" + (
            "<span class='chip'>inherited from project</span>" if not own and eff != "private" else ""
        ) + "</div>"
        body = head + f"<div class='md'>{self._dash_links(render_markdown(body_md, path))}</div>"
        return self._brain_shell(title, body, crumbs, self._brain_sidebar(projects, active=proj), active_tab="home")

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

    # -- rooms (v3 Wave 6 — the live-room web surface) -----------------------

    def _budget_meter(self, used: int, budget: int) -> str:
        pct = min(100, int(100 * used / budget)) if budget else 0
        color = "var(--red)" if pct >= 90 else ("var(--amber)" if pct >= 70 else "var(--accent)")
        return (
            f"<div class='meta' style='font-size:12px'>{used}/{budget} messages</div>"
            "<div style='background:var(--surface-2);border-radius:6px;overflow:hidden;height:6px;margin:.2rem 0 .6rem'>"
            f"<div style='width:{pct}%;height:100%;background:{color}'></div></div>"
        )

    def _render_room_card(self, row: dict, handles: dict[int, str]) -> str:
        from engram_server.explorer.html import badge, esc

        members_html = "".join(
            self._avatar_for(handles.get(uid, str(uid)), size=22) for uid in row["member_ids"][:6]
        )
        status = badge("open", "active") if row["status"] == "open" else badge("closed", "done")
        unread = f"<span class='badge unread'>{row['unread']}</span>" if row.get("unread") else ""
        return (
            f"<a class='card' href='/dashboard/rooms/{esc(row['name'])}'>"
            f"<h3>{esc(row['name'])} {unread}</h3>"
            f"<p class='desc'>{esc(row['goal'])}</p>"
            f"<div class='stat-row'>{status}<span class='chip'>{members_html}</span></div>"
            + self._budget_meter(row.get("messages_used", 0), row["turn_budget"])
            + "</a>"
        )

    def _render_rooms_list(self, rows: list[dict], handles: dict[int, str], notice: str = "", error: str = "") -> str:
        from engram_server.explorer.html import esc

        banner = (f"<p class=notice>{esc(notice)}</p>" if notice else "") + (
            f"<p class=error>{esc(error)}</p>" if error else ""
        )
        parts = [banner, "<div class='page-head'><div><p class='eyebrow'>Live rooms</p><h1>Rooms</h1></div></div>"]
        if not rows:
            parts.append(
                "<div class='empty-cta'><b>No rooms yet.</b><br>"
                "A room is a live space where your Claude and a teammate's Claude work "
                "something out together — and can search whatever each side chooses to "
                "share, for the life of the room only.<br>Open one below, or from any "
                "profile via <i>Open a room</i>.</div>")
        else:
            parts.append("<div class='cards'>" + "".join(self._render_room_card(r, handles) for r in rows) + "</div>")
        parts.append(
            "<p class='section-label'>New room</p>"
            "<div class='card newroom'><form method=post action='/dashboard/rooms'>"
            "<p>Name<br><input name=name pattern='[a-z0-9][a-z0-9-]{2,63}' required "
            "placeholder='design-review'></p>"
            "<p>Goal — rooms have a turn budget, so give it one<br><input name=goal required "
            "placeholder='What should this room accomplish?'></p>"
            "<p>Exit condition (optional)<br><input name=exit_condition "
            "placeholder='e.g. agreement on the API shape'></p>"
            "<p>Invite (comma-separated handles)<br><input name=invite "
            "placeholder='alice, bob'></p>"
            "<button type=submit>Open room</button></form></div>"
        )
        return "".join(parts)

    async def rooms_list(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        if me is None:
            return HTMLResponse(await self._social_shell(session, "Rooms",
                "<p class='empty'>No account.</p>", [("home", "/dashboard"), ("rooms", "/dashboard/rooms")],
                active_tab="rooms"))
        handles = self.registry.tenancy_handle_map()
        rows = self.registry.rooms.list_rooms_for(me.id, include_closed=True)
        body = self._render_rooms_list(
            rows, handles,
            notice=str(request.query_params.get("notice", "")),
            error=str(request.query_params.get("error", "")),
        )
        return HTMLResponse(await self._social_shell(session, "Rooms", body,
            [("home", "/dashboard"), ("rooms", "/dashboard/rooms")], active_tab="rooms"))

    async def create_room(self, request: "Request") -> "Response":
        from urllib.parse import quote

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        if me is None:
            return RedirectResponse("/dashboard/rooms", status_code=302)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        goal = str(form.get("goal", "")).strip()
        exit_condition = str(form.get("exit_condition", "")).strip()
        invite_handles = str(form.get("invite", "")).strip()
        try:
            room = self.registry.rooms.open_room(me.id, name, goal, exit_condition=exit_condition)
        except TeamworkError as exc:
            return RedirectResponse(f"/dashboard/rooms?error={quote(str(exc))}", status_code=302)
        for h in (x.strip().lstrip("@") for x in invite_handles.split(",")):
            if not h:
                continue
            other = self.registry.tenancy.user_by_handle(h)
            if other is None or other.id == me.id:
                continue
            if self.registry.rooms.invite(room.id, me.id, other.id):
                self._push_notification(
                    other.id, "room_invite",
                    f"@{me.handle} invited you to room '{room.name}': {room.goal[:120]}",
                    ref=room.name,
                )
        return RedirectResponse(f"/dashboard/rooms/{room.name}", status_code=302)

    async def open_room_with(self, request: "Request") -> "Response":
        """The profile page's "Open a room with @x" button — a direct 1:1 room,
        named deterministically-but-uniquely so two people can start one anytime."""
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        form = await request.form()
        handle = str(form.get("handle", "")).strip().lstrip("@")
        other = self.registry.tenancy.user_by_handle(handle)
        if me is None or other is None or other.id == me.id:
            return RedirectResponse(self._safe_back(request, "/dashboard/people"), status_code=302)
        name = f"{me.handle}-{other.handle}-{secrets.token_hex(2)}"
        try:
            room = self.registry.rooms.open_room(
                me.id, name, f"Direct room between @{me.handle} and @{other.handle}"
            )
            self.registry.rooms.invite(room.id, me.id, other.id)
        except TeamworkError:
            return RedirectResponse(self._safe_back(request, f"/dashboard/u/{other.handle}"), status_code=302)
        self._push_notification(
            other.id, "room_invite", f"@{me.handle} invited you to room '{room.name}': {room.goal}",
            ref=room.name,
        )
        return RedirectResponse(f"/dashboard/rooms/{room.name}", status_code=302)

    def _render_room_bubbles(self, turns: list, handles: dict[int, str], me) -> str:
        from engram_server.explorer.format import humanize_time
        from engram_server.explorer.html import esc
        from engram_server.explorer.render import render_markdown_public

        order: dict[int, int] = {}
        parts: list[str] = []
        for t in turns:
            if t.kind == "system":
                parts.append(f"<p class='meta' style='text-align:center'>{esc(t.body)}</p>")
                continue
            if t.kind == "guest_read":
                parts.append(f"<p class='meta' style='font-size:12px;font-style:italic'>{esc(t.body)}</p>")
                continue
            idx = order.setdefault(t.user_id, len(order))
            side = "right" if (me is not None and t.user_id == me.id) else "left"
            color = idx % 4
            handle = handles.get(t.user_id, str(t.user_id))
            rel, exact = humanize_time(t.created)
            body_html = render_markdown_public(t.body)
            sess = t.session or ""
            via_tag = (
                "<span class='via-chip' title='written by their Claude'>🤖 Claude</span>"
                if sess == "claude" else ""
            )
            parts.append(
                f"<div class='bubble {side} c{color}'>"
                f"<div class='bhead'>{self._avatar_for(handle, size=18)} "
                f"<span class='bsender'>@{esc(handle)}</span>{via_tag}"
                f"<span class='btime' title='{esc(exact)}'>{esc(rel)}</span></div>"
                f"<div class='bbody md'>{body_html}</div></div>"
            )
        return "".join(parts)

    def _render_room_transcript(
        self, room, turns: list, members: list[dict], grants: list[dict],
        handles: dict[int, str], me, notice: str = "", error: str = "",
    ) -> str:
        from engram_server.explorer.html import badge, esc

        banner = (f"<p class=notice>{esc(notice)}</p>" if notice else "") + (
            f"<p class=error>{esc(error)}</p>" if error else ""
        )
        status = badge("open", "active") if room.status == "open" else badge("closed", "done")
        head = (
            "<div class='page-head'><div><p class='eyebrow'>Room</p>"
            f"<h1>{esc(room.name)}</h1><p class='desc'>{esc(room.goal)}</p>"
            + (f"<p class='meta'>Exit condition: {esc(room.exit_condition)}</p>" if room.exit_condition else "")
            + "</div></div>"
        )
        stat_row = f"<div class='stat-row'>{status}</div>" + self._budget_meter(
            sum(1 for t in turns if t.kind == "message") or 0, room.turn_budget
        )
        member_chips = "".join(
            f"<span class='chip'>{self._avatar_for(handles.get(m['user_id'], str(m['user_id'])), size=18)} "
            f"@{esc(handles.get(m['user_id'], str(m['user_id'])))}</span>"
            for m in members
        )
        grant_items = "".join(
            f"<li>@{esc(handles.get(g['grantor_id'], str(g['grantor_id'])))} shared "
            f"<code>{esc(g['path_prefix'])}</code></li>"
            for g in grants
        ) or "<li class='empty'>No context shared in this room.</li>"
        bubbles = self._render_room_bubbles(turns, handles, me)
        outcome_html = (
            f"<p class='meta'><b>Outcome:</b> {esc(room.outcome) if room.outcome else '(none given)'}</p>"
            if room.status == "closed" else ""
        )
        controls = ""
        if room.status == "open":
            controls = (
                f"<form class='composer' method='post' action='/dashboard/rooms/{esc(room.name)}/post'>"
                "<textarea name='body' required placeholder='Message this room…'></textarea>"
                "<button type=submit>Send</button></form>"
                f"<form class='closeform' method='post' action='/dashboard/rooms/{esc(room.name)}/close'>"
                "<input name='outcome' placeholder='Outcome — what did this room decide? (optional)'>"
                "<button type=submit>Close room</button></form>"
            )
        last_id = turns[-1].id if turns else 0
        poller = (
            "<script>(function(){"
            f"var since={last_id};var name={room.name!r};"
            "var box=document.getElementById('room-transcript');"
            "function esc(s){var d=document.createElement('div');d.innerText=s;return d.innerHTML;}"
            "function poll(){"
            "if(document.visibilityState!=='visible')return;"
            "fetch('/dashboard/api/rooms/'+name+'.json?since='+since)"
            ".then(function(r){return r.json();}).then(function(d){"
            "if(!d.turns)return;"
            "d.turns.forEach(function(t){"
            "since=t.id;"
            "var div=document.createElement('div');"
            "div.className='bubble left c0';"
            "div.innerHTML='<div class=\"bhead\"><span class=\"bsender\">@'+esc(t.author)+'</span>'"
            "+(t.via==='claude'?'<span class=\"via-chip\">🤖 Claude</span>':'')+'</div>'"
            "+'<div class=\"bbody\">'+esc(t.body)+'</div>';"
            "box.appendChild(div);"
            "});"
            "}).catch(function(){});"
            "}"
            "setInterval(poll,5000);"
            "})();</script>"
        )
        return (
            banner + head + stat_row
            + f"<p class='section-label'>Members</p><div class='stat-row'>{member_chips}</div>"
            + f"<p class='section-label'>Shared context</p><ul>{grant_items}</ul>"
            + f"<p class='section-label'>Conversation</p>"
            + f"<div id='room-transcript' class='thread-transcript'>{bubbles}</div>"
            + outcome_html + controls + poller
        )

    async def room_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        name = str(request.path_params.get("name", ""))
        room = self.registry.rooms.room_by_name(name)
        if room is None or me is None:
            return HTMLResponse(await self._social_shell(session, "Not found",
                "<p class='empty'>No such room.</p>", [("home", "/dashboard"), ("rooms", "/dashboard/rooms")],
                active_tab="rooms"), status_code=404)
        try:
            # limit above the max hard_cap (500 messages) plus headroom for system turns
            # (open/close/etc.) so the initial load always shows the whole conversation.
            turns = self.registry.rooms.read_turns(room.id, me.id, since_id=0, limit=1000)
        except TeamworkError:
            return HTMLResponse(await self._social_shell(session, "Not a member",
                "<p class='empty'>You're not a member of this room.</p>",
                [("home", "/dashboard"), ("rooms", "/dashboard/rooms")], active_tab="rooms"), status_code=403)
        handles = self.registry.tenancy_handle_map()
        members = self.registry.rooms.members(room.id)
        grants = self.registry.rooms.grants_for(room.id)
        body = self._render_room_transcript(
            room, turns, members, grants, handles, me,
            notice=str(request.query_params.get("notice", "")),
            error=str(request.query_params.get("error", "")),
        )
        return HTMLResponse(await self._social_shell(
            session, f"Room: {room.name}", body,
            [("home", "/dashboard"), ("rooms", "/dashboard/rooms"), (room.name, f"/dashboard/rooms/{room.name}")],
            active_tab="rooms"))

    async def post_room_turn(self, request: "Request") -> "Response":
        from urllib.parse import quote

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        name = str(request.path_params.get("name", ""))
        room = self.registry.rooms.room_by_name(name)
        if room is None or me is None:
            return RedirectResponse("/dashboard/rooms", status_code=302)
        form = await request.form()
        body = str(form.get("body", "")).strip()
        findings = _scan_secrets(body)
        if findings:
            kinds = sorted({k for k, _ in findings})
            msg = f"Refusing to post: this looks like it contains a secret ({', '.join(kinds)})."
            return RedirectResponse(f"/dashboard/rooms/{quote(room.name)}?error={quote(msg)}", status_code=302)
        try:
            self.registry.rooms.post_turn(room.id, me.id, body, session=f"dashboard:{me.handle}")
        except TeamworkError as exc:
            return RedirectResponse(f"/dashboard/rooms/{quote(room.name)}?error={quote(str(exc))}", status_code=302)
        try:
            from engram_server.teamwork import room_notify

            await room_notify(room.id)
        except Exception:  # noqa: BLE001 — also covers ImportError if not shipped yet
            pass
        return RedirectResponse(f"/dashboard/rooms/{quote(room.name)}", status_code=302)

    async def close_room_route(self, request: "Request") -> "Response":
        from urllib.parse import quote

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        name = str(request.path_params.get("name", ""))
        room = self.registry.rooms.room_by_name(name)
        if room is None or me is None:
            return RedirectResponse("/dashboard/rooms", status_code=302)
        form = await request.form()
        outcome = str(form.get("outcome", "")).strip()
        try:
            self.registry.rooms.close_room(room.id, me.id, outcome)
        except TeamworkError as exc:
            return RedirectResponse(f"/dashboard/rooms/{quote(room.name)}?error={quote(str(exc))}", status_code=302)
        return RedirectResponse(f"/dashboard/rooms/{quote(room.name)}", status_code=302)

    async def api_room_json(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        session = self._session(request)
        me = self._session_user(session) if session is not None else None
        if me is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        name = str(request.path_params.get("name", ""))
        room = self.registry.rooms.room_by_name(name)
        if room is None:
            return JSONResponse({"ok": False, "error": "no such room"}, status_code=404)
        try:
            since = int(request.query_params.get("since", 0) or 0)
        except ValueError:
            since = 0
        try:
            turns = self.registry.rooms.read_turns(room.id, me.id, since_id=since, limit=200)
        except TeamworkError:
            return JSONResponse({"ok": False, "error": "not a member"}, status_code=403)
        handles = self.registry.tenancy_handle_map()
        out = [
            {"id": t.id, "author": handles.get(t.user_id, str(t.user_id)), "kind": t.kind,
             "body": t.body, "created": t.created,
             "via": ("human" if ((t.session or "").startswith("dashboard:") or (t.session or "") in ("web","app")) else ("claude" if (t.session or "") == "claude" else ""))}
            for t in turns
        ]
        cursor = out[-1]["id"] if out else since
        return JSONResponse({"ok": True, "turns": out, "cursor": cursor, "status": room.status})

    # -- team API + presence (v3 Wave 6) --------------------------------------

    async def api_team(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        session = self._session(request)
        me = self._session_user(session) if session is not None else None
        if me is None:
            me = self._bearer_user(request)
        if me is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        umap = {u.id: u for u in self.registry.tenancy.list_users()}
        team = []
        for row in self.registry.presence.roster():
            u = umap.get(row["user_id"])
            if u is None:
                continue
            team.append({
                "handle": u.handle, "display_name": u.display_name or "", "avatar_url": u.avatar_url or "",
                "project": row["project"], "tool": row["tool"], "minutes_ago": row["minutes_ago"],
            })
        self_row = self.registry.presence.self_row(me.id)
        return JSONResponse({
            "ok": True, "team": team,
            "me": {"handle": me.handle, "invisible": bool(self_row and self_row["invisible"])},
        })

    async def api_set_presence(self, request: "Request") -> "Response":
        from starlette.responses import JSONResponse

        session = self._session(request)
        me = self._session_user(session) if session is not None else None
        if me is None:
            me = self._bearer_user(request)
        if me is None:
            return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 — malformed body just means "no change requested"
            data = {}
        invisible = bool(data.get("invisible"))
        self.registry.presence.set_invisible(me.id, invisible)
        return JSONResponse({"ok": True, "invisible": invisible})

    async def office_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        me = self._session_user(session)
        handles = self.registry.tenancy_handle_map()
        rooms_html = "<p class='empty'>No open rooms.</p>"
        if me is not None:
            # Shadow rooms carry thread floor state and are plumbing, not
            # conversations — listing them would show a phantom room beside every
            # thread. Same filter the MCP kb_rooms applies.
            rows = [
                r for r in self.registry.rooms.list_rooms_for(me.id)
                if not str(r["name"]).startswith("thread--")
            ]
            if rows:
                rooms_html = "<div class='cards'>" + "".join(
                    self._render_room_card(r, handles) for r in rows) + "</div>"
        owner_link = (
            "<p class='meta'>The pixel office floor lives in the claude.ai app — "
            "say <i>open the office</i> in any chat.</p>"
            if self._is_owner(session) else ""
        )
        body = (
            "<div class='page-head'><div><p class='eyebrow'>Team</p><h1>Office</h1></div></div>"
            + (self._render_working_strip() or "<p class='empty'>No one online right now.</p>")
            + (await self._render_working_on(me.handle) if me is not None else "")
            + "<p class='section-label'>Open rooms</p>" + rooms_html
            + owner_link
        )
        return HTMLResponse(await self._social_shell(session, "Office", body,
            [("home", "/dashboard"), ("office", "/dashboard/office")], active_tab="office"))

    # -- artifacts + setup (parity pages) -------------------------------------

    def _render_artifacts(self, artifacts: list) -> str:
        from engram_server.explorer.html import badge, esc

        parts = ["<div class='page-head'><div><p class='eyebrow'>Your work</p><h1>Artifacts</h1></div></div>"]
        if not artifacts:
            parts.append("<p class='empty'>No artifacts yet — build one from your Claude with build_artifact.</p>")
            return "".join(parts)
        cards = []
        for a in artifacts:
            stale = a.get("stale")
            chip_html = (
                badge("stale", "priority-high") if stale is True
                else badge("current", "active") if stale is False else ""
            )
            path = str(a.get("path") or "")
            cards.append(
                f"<a class='card' href='/dashboard/f/{esc(path)}'><h3>{esc(a.get('title') or path)}</h3>"
                + (f"<p class='desc'>{esc(a['description'])}</p>" if a.get("description") else "")
                + f"<div class='stat-row'>{chip_html}</div></a>"
            )
        parts.append(f"<div class='cards'>{''.join(cards)}</div>")
        return "".join(parts)

    async def artifacts_view(self, request: "Request") -> "Response":
        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        store = await self._user_store(session)
        if store is None:
            return HTMLResponse(self._page("No account", "<p>No brain to browse.</p>"), status_code=403)
        artifacts = await store.kb_artifacts()
        body = self._subtabs(self._BROWSE_SUBTABS, "/dashboard/artifacts") + self._render_artifacts(artifacts)
        return HTMLResponse(await self._social_shell(
            session, "Artifacts", body, [("home", "/dashboard"), ("artifacts", "/dashboard/artifacts")],
            active_tab="browse"))

    async def setup_view(self, request: "Request") -> "Response":
        from engram_server.explorer.html import esc

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        url = f"{self.settings.public_url}/mcp"
        body = (
            "<div class='page-head'><div><p class='eyebrow'>Connect</p><h1>Set up your Engram</h1></div></div>"
            "<div class=card><h2>claude.ai / mobile</h2>"
            f"<p>Settings → Connectors → Add custom connector → <span class=mono>{esc(url)}</span> → sign in.</p></div>"
            "<div class=card><h2>Claude Code</h2>"
            f"<p><span class=mono>claude mcp add --transport http engram {esc(url)}</span></p></div>"
            "<div class=card><h2>ChatGPT</h2>"
            f"<p>Settings → Connectors → add <span class=mono>{esc(url)}</span> "
            "(needs a plan with custom MCP connectors).</p></div>"
            "<div class=card><h2>Desktop app — Engram in your tray (recommended)</h2>"
            "<p>Native notifications even with no browser open, plus the team's live "
            "'working now' roster in your Windows taskbar / Mac menu bar. Clicks open "
            "this site — one UI, everywhere.</p>"
            "<p><a href='https://github.com/metalfinger/engram/releases/latest'><b>⬇ Download "
            "for Windows (.msi) / macOS (.dmg) / Linux</b></a></p>"
            "<ol><li>Install (Windows: SmartScreen → More info → Run anyway; "
            "macOS: right-click → Open the first time — unsigned for now)</li>"
            "<li>Click the Engram tray icon → <b>Sign in…</b> — your browser opens, "
            "one click, done. No tokens.</li></ol></div>"
            "<div class=card><h2>Chrome extension — notifications on your desktop</h2>"
            "<p>Room invites, DMs, and questions reach you even with no chat open, plus the "
            "team's live 'working now' list.</p>"
            "<p><a href='/downloads/engram-chrome-extension.zip'><b>⬇ Download the extension"
            "</b></a> and unzip it, then:</p>"
            "<ol><li>Open <span class=mono>chrome://extensions</span></li>"
            "<li>Turn on <b>Developer mode</b> (top right)</li>"
            "<li><b>Load unpacked</b> → pick the unzipped <span class=mono>engram-chrome-extension"
            "</span> folder</li>"
            "<li>Click the Engram icon in the toolbar → <b>Sign in with Engram</b> — same "
            "account as everything else, no tokens to paste</li></ol></div>"
            "<div class=card><h2>Teach claude.ai the Engram protocol (recommended)</h2>"
            "<p>Claude Code learns Engram automatically (the connector carries its "
            "instructions + the skill syncs). The claude.ai website & apps need ONE "
            "of these two, once:</p>"
            "<p><b>Best — upload the skill:</b> "
            "<a href='/downloads/engram-skill.zip'>⬇ download engram-skill.zip</a>, then in "
            "claude.ai: <span class=mono>Settings → Capabilities → Skills → Upload skill</span> "
            "(needs code execution enabled; Pro/Max/Team). Claude then loads the full Engram "
            "protocol only when a chat touches Engram — zero cost otherwise.</p>"
            "<details><summary><b>Alternative — paste into Project instructions or your "
            "profile preferences</b></summary><div class=codebox style='white-space:pre-wrap'>"
            "You have the Engram connector — my persistent memory and my team's shared brain. "
            "At the start of work: kb_load(project) (kb_projects lists them); surface unread "
            "messages first. Navigate, never ingest: read indexes, then single files with "
            "kb_read. Write decisions with kb_write as they happen; log the session at close "
            "(kb_append_log). Before solving hard problems, kb_explore(query=...) — a teammate "
            "may have solved it. kb_team() = who's working. Rooms: "
            "kb_room_post(room, msg, wait_for_reply=True) — never poll in a loop. A closed "
            "room's outcome is only saved to my brain after I say yes."
            "</div></details></div>"
            "<div class=card><h2>Bring your history</h2>"
            "<p>Already have ChatGPT or Claude history? <span class=mono>kb_import</span> can backfill it from "
            "an export — anything it imports lands PRIVATE, never shared automatically.</p></div>"
            "<div class=card><h2>Team presence</h2>"
            "<p>Other signed-in Engram users can see that you're online and what project you're in "
            "(see <a href='/dashboard/office'>Office</a>) — turn that off anytime with the invisible "
            "toggle there.</p></div>"
        )
        return HTMLResponse(await self._social_shell(
            session, "Setup", body, [("home", "/dashboard"), ("setup", "/dashboard/setup")]))

    # -- ops (owner-only) ------------------------------------------------------

    async def ops_view(self, request: "Request") -> "Response":
        from engram_server.explorer.html import esc

        session = self._session(request)
        if session is None:
            return RedirectResponse("/dashboard/login", status_code=302)
        if not self._is_owner(session):
            return PlainTextResponse("Owner only.", status_code=403)
        users = self.registry.tenancy.list_users()
        active_24h = self.registry.presence.roster(active_minutes=24 * 60)
        seen_rooms: dict[int, dict] = {}
        for u in users:
            for row in self.registry.rooms.list_rooms_for(u.id, include_closed=True):
                seen_rooms[row["id"]] = row
        open_rooms = sum(1 for r in seen_rooms.values() if r["status"] == "open")
        closed_rooms = sum(1 for r in seen_rooms.values() if r["status"] == "closed")
        asks_open = asks_answered = 0
        unread_notifs = 0
        for u in users:
            for a in self.registry.discovery.list_asks_for(u.id, open_only=False):
                if a.status == "open":
                    asks_open += 1
                else:
                    asks_answered += 1
            unread_notifs += self.registry.social.unread_counts(u.id)["notifications"]
        active_users = sum(1 for u in users if u.status == "active")
        # PRD §10 — the numbers that decide the team test at two weeks.
        reads = self.registry.discovery.public_read_counts()
        counts_html = (
            "<table><tbody>"
            f"<tr><td>Users (total/active)</td><td>{len(users)}/{active_users}</td></tr>"
            f"<tr><td>Presence-active (24h)</td><td>{len(active_24h)}</td></tr>"
            f"<tr><td>Rooms (open/closed)</td><td>{open_rooms}/{closed_rooms}</td></tr>"
            f"<tr><td>Asks (open/answered)</td><td>{asks_open}/{asks_answered}</td></tr>"
            f"<tr><td>Cross-user reads (total / distinct readers)</td>"
            f"<td>{reads['total']}/{reads['distinct_readers']}</td></tr>"
            f"<tr><td>Unread notifications</td><td>{unread_notifs}</td></tr>"
            "</tbody></table>"
        )
        rows_html = ""
        for u in users:
            sr = self.registry.presence.self_row(u.id)
            last_seen = sr["updated"] if sr else "never"
            invisible = "yes" if (sr and sr["invisible"]) else "no"
            rows_html += (
                f"<tr><td>@{esc(u.handle)}</td><td>{esc(u.created)}</td>"
                f"<td>{esc(last_seen)}</td><td>{invisible}</td></tr>"
            )
        users_html = (
            "<table><thead><tr><th>Handle</th><th>Created</th><th>Last presence</th>"
            "<th>Invisible?</th></tr></thead><tbody>" + rows_html + "</tbody></table>"
        )
        body = (
            "<div class='page-head'><div><p class='eyebrow'>Operator</p><h1>Ops</h1></div></div>"
            "<div class=card><h2>Counts</h2>" + counts_html + "</div>"
            "<div class=card><h2>Users</h2>" + users_html + "</div>"
        )
        return HTMLResponse(await self._social_shell(
            session, "Ops", body, [("home", "/dashboard"), ("ops", "/dashboard/ops")]))


def _same_origin_guard(settings: "Settings"):
    """CSRF defense-in-depth for state-changing POSTs (sec-review, v3).

    The one-door session cookie is Domain-wide (.metalfinger.xyz) so both hostnames
    share the login — but SameSite=Lax does NOT block a POST from a SIBLING
    subdomain (same registrable domain = same-site). If any other subdomain were
    ever attacker-influenceable (dangling CNAME, a forgotten preview deploy), it
    could auto-submit forms with the victim's cookie. So: when a browser sends an
    Origin header, it must match one of OUR two origins exactly, or the POST is
    refused. Requests WITHOUT an Origin (curl, tests, non-browser clients) pass —
    they can't ride a browser's cookie jar, which is the only thing this defends.
    """
    from urllib.parse import urlsplit as _split

    def _origin_of(url: str) -> str:
        p = _split(url or "")
        return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""

    allowed = {o for o in (_origin_of(settings.public_url), _origin_of(settings.explorer_url)) if o}

    def wrap(handler):
        async def guarded(request: "Request") -> "Response":
            origin = (request.headers.get("origin") or "").strip().lower()
            if origin and origin != "null" and allowed and origin not in allowed:
                return PlainTextResponse(
                    "Cross-origin request refused.", status_code=403
                )
            return await handler(request)

        return guarded

    return wrap


def register_dashboard(mcp, settings: "Settings", registry: "StoreRegistry", idps: dict) -> Dashboard | None:
    """Wire the dashboard + onboarding routes. No-op (returns None) unless multiuser
    is on and at least one IdP is configured — single-user deployments never expose it."""
    if not settings.multiuser or not idps:
        return None
    dash = Dashboard(settings, registry, idps)
    csrf = _same_origin_guard(settings)

    mcp.custom_route("/dashboard", ["GET"])(dash.dashboard)
    mcp.custom_route("/dashboard/login", ["GET"])(dash.login)
    mcp.custom_route("/dashboard/auth/{idp}", ["GET"])(dash.start_oauth)
    mcp.custom_route(CALLBACK_PATH, ["GET"])(dash.callback)
    mcp.custom_route("/join", ["GET"])(dash.join)
    mcp.custom_route("/join/claim", ["POST"])(csrf(dash.claim))
    mcp.custom_route("/dashboard/invite", ["POST"])(csrf(dash.create_invite))
    mcp.custom_route("/dashboard/invite/github", ["POST"])(csrf(dash.create_github_invite))
    mcp.custom_route("/dashboard/invite/revoke", ["POST"])(csrf(dash.revoke_invite))
    mcp.custom_route("/dashboard/logout", ["POST"])(csrf(dash.logout))
    mcp.custom_route("/dashboard/profile", ["POST"])(csrf(dash.save_profile))
    mcp.custom_route("/dashboard/p/{project}", ["GET"])(dash.browse_project)
    mcp.custom_route("/dashboard/p/{project}/move", ["POST"])(csrf(dash.move_project_action))
    mcp.custom_route("/dashboard/f/{path:path}", ["GET"])(dash.browse_concept)
    mcp.custom_route("/dashboard/people", ["GET"])(dash.people_view)
    mcp.custom_route("/dashboard/feed", ["GET"])(dash.feed_view)
    mcp.custom_route("/dashboard/asks", ["GET"])(dash.asks_view)
    mcp.custom_route("/dashboard/u/{handle}", ["GET"])(dash.profile_view)
    mcp.custom_route("/dashboard/u/{handle}/f/{path:path}", ["GET"])(dash.public_concept_view)
    mcp.custom_route("/dashboard/follow", ["POST"])(csrf(dash.follow_action))
    mcp.custom_route("/dashboard/ask", ["POST"])(csrf(dash.ask_action))
    mcp.custom_route("/dashboard/asks/answer", ["POST"])(csrf(dash.answer_action))
    mcp.custom_route("/dashboard/search", ["GET"])(dash.browse_search)
    mcp.custom_route("/dashboard/activity", ["GET"])(dash.browse_activity)
    mcp.custom_route("/dashboard/graph", ["GET"])(dash.browse_graph)
    mcp.custom_route("/dashboard/contact/add", ["POST"])(csrf(dash.add_contact))
    mcp.custom_route("/dashboard/contact/accept", ["POST"])(csrf(dash.accept_contact))
    mcp.custom_route("/dashboard/notifications/read", ["POST"])(csrf(dash.read_notifications))
    # NOT /dashboard/api/presence — that POST is already the invisible toggle, and
    # registering a second handler on it would silently shadow the tray's.
    mcp.custom_route("/dashboard/api/presence/upload", ["POST"])(dash.api_presence_upload)
    mcp.custom_route("/dashboard/api/asks", ["GET"])(dash.api_asks)
    mcp.custom_route("/dashboard/api/asks/answer", ["POST"])(dash.api_answer_ask)
    mcp.custom_route("/dashboard/api/notifications", ["GET"])(dash.api_notifications)
    mcp.custom_route("/dashboard/api/notifications/read", ["POST"])(csrf(dash.api_mark_read))
    mcp.custom_route("/dashboard/ext-auth", ["GET"])(dash.ext_auth)
    mcp.custom_route("/dashboard/rooms", ["GET"])(dash.rooms_list)
    mcp.custom_route("/dashboard/rooms", ["POST"])(csrf(dash.create_room))
    mcp.custom_route("/dashboard/rooms/open-with", ["POST"])(csrf(dash.open_room_with))
    mcp.custom_route("/dashboard/rooms/{name}", ["GET"])(dash.room_view)
    mcp.custom_route("/dashboard/rooms/{name}/post", ["POST"])(csrf(dash.post_room_turn))
    mcp.custom_route("/dashboard/rooms/{name}/close", ["POST"])(csrf(dash.close_room_route))
    mcp.custom_route("/dashboard/api/rooms/{name}.json", ["GET"])(dash.api_room_json)
    mcp.custom_route("/dashboard/api/team", ["GET"])(dash.api_team)
    mcp.custom_route("/dashboard/api/presence", ["POST"])(csrf(dash.api_set_presence))
    mcp.custom_route("/dashboard/office", ["GET"])(dash.office_view)
    mcp.custom_route("/dashboard/artifacts", ["GET"])(dash.artifacts_view)
    mcp.custom_route("/dashboard/setup", ["GET"])(dash.setup_view)
    mcp.custom_route("/dashboard/ops", ["GET"])(dash.ops_view)
    return dash
