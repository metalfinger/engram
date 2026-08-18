"""Engram FastMCP app: the 8 kb_* tools + OAuth wall + transparency explorer.

Module-level construction (like the Survey MCP): settings, KBStore, optional
ProxyOAuthProvider, FastMCP instance, tool registration, explorer routes.
``main()`` prepares the brain checkout and serves streamable HTTP.

The tool DOCSTRINGS are the product: for claude.ai sessions (where no skill can
be installed) they carry the entire Engram protocol.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from anyio import to_thread
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from engram_server import session_prep
from engram_server.config import Settings, get_settings
from engram_server.doctor import run_doctor
from engram_server.errors import GitError, KBError
from engram_server.explore_widget import (
    explore_app_tool_meta,
    explore_tool_meta,
    register_explore_widget,
)
from engram_server.frontmatter import split as fm_split
from engram_server.explorer import register as register_explorer
from engram_server.kbstore import KBStore, _scan_secrets
from engram_server.meetings_widget import (
    meeting_reply as _meeting_reply_impl,
)
from engram_server.meetings_widget import (
    meetings_app_tool_meta,
    meetings_payload,
    meetings_tool_meta,
    register_meetings_widget,
)
from engram_server.app_widget import (
    app_launcher_meta,
    app_only_meta,
    register_app_widget,
)
from engram_server.navigator import navigator_tool_meta, register_navigator
from engram_server.office_widget import register_office_widget
from engram_server.social_widget import (
    register_social_widget,
    social_app_tool_meta,
    social_tool_meta,
)
from engram_server.oauth.idp import get_idp
from engram_server.oauth.provider import LoginNotAllowedError, ProxyOAuthProvider, handle_callback
from engram_server import limits, notify
from engram_server.dashboard import register_dashboard
from engram_server.explorer.homepage import register_homepage
from engram_server.oauth.store import InMemoryOAuthStore
from engram_server.registry import StoreRegistry
from mcp.server.auth.middleware.auth_context import get_access_token
from engram_server.scheduler import start_schedulers

log = logging.getLogger("engram.app")

settings = get_settings()
registry = StoreRegistry(settings)
# Owner-scoped surfaces: explorer + web routes (Cloudflare Access = Hiren-only),
# scheduler, presence spool, and startup bring-up. Tool bodies must NEVER touch
# this global directly — they resolve through current_store() (M0.4).
store = registry.owner


async def current_store() -> KBStore:
    """M0.4 authz seam — the ONLY way a tool body picks a store.

    The SDK's bearer middleware stashes each request's validated AccessToken in a
    ContextVar; ``.subject`` is the identity the OAuth proxy minted
    ("github:metalfinger"). subject -> account -> that user's store, via the
    registry. No token in context (auth disabled: localhost dev, tests) resolves
    to the owner store, as does multiuser=False — the pre-M0 behavior exactly.

    Tool bodies use the ``await (await current_store()).kb_x(...)`` shape: the
    first await resolves WHO is calling, the second does the work.
    """
    token = get_access_token()
    if token is None or not token.subject:
        return registry.owner
    resolved = await registry.store_for_subject(token.subject)
    if resolved is not registry.owner:
        # Per-tenant quota + rate limit (M0.7). Skipped for the owner store
        # (returned in single-user mode and for owner subjects/handle), so the
        # operator is never throttled or quota-capped.
        limits.enforce(resolved, token.subject, settings)
    _touch_presence(token.subject)
    return resolved


# v3 Wave 3: team presence derived from tool calls. The server sees every kb_*
# call with the caller's identity — zero setup for teammates (the hook->spool
# path only ever worked on the operator's own machines). Throttled in-memory so
# presence costs one SQLite upsert per user per minute, not per call. The tool
# name comes from the calling frame (tool bodies call current_store directly);
# frame introspection is best-effort — '' on any surprise. Project attribution
# is explicit, not guessed: kb_load / kb_attach_project call _presence_project.
_PRESENCE_THROTTLE_S = 60.0
_presence_last: dict[int, float] = {}


def _touch_presence(subject: str) -> None:
    if not settings.multiuser:
        return
    try:
        user = registry.tenancy.user_by_subject(subject)
        if user is None:
            return
        now = time.monotonic()
        if now - _presence_last.get(user.id, 0.0) < _PRESENCE_THROTTLE_S:
            return
        _presence_last[user.id] = now
        tool = ""
        try:
            tool = sys._getframe(2).f_code.co_name  # noqa: SLF001 — best-effort label
        except Exception:  # noqa: BLE001
            tool = ""
        if not tool.startswith("kb_"):
            tool = ""
        registry.presence.touch(user.id, tool=tool)
    except Exception:  # noqa: BLE001 — presence must NEVER break a tool call
        log.debug("presence touch failed", exc_info=True)


def _presence_project(project: str) -> None:
    """Explicit project attribution (called by kb_load / kb_attach_project)."""
    if not settings.multiuser:
        return
    try:
        user = current_user()
        if user is not None and project:
            registry.presence.touch(user.id, tool="kb_load", project=project)
    except Exception:  # noqa: BLE001
        log.debug("presence project set failed", exc_info=True)


def current_user():
    """M2 social identity — resolve the caller to their tenancy account (User), or
    None when there's no auth context (localhost/tests) or no account. Distinct from
    current_store(): social tools key on WHO you are, not which brain you own. The
    owner is bootstrapped into an account at startup so they appear here too."""
    token = get_access_token()
    if token is None or not token.subject:
        return None
    return registry.tenancy.user_by_subject(token.subject)


# ------------------------------------------------------------------ auth (optional)


def _idp_creds_present(s: Settings) -> bool:
    """True when the configured upstream IdP has both client id and secret."""
    if s.oauth_provider == "github":
        return bool(s.github_client_id and s.github_client_secret)
    if s.oauth_provider == "google":
        return bool(s.google_client_id and s.google_client_secret)
    return False


_AUTH_ENABLED = _idp_creds_present(settings)
_provider: ProxyOAuthProvider | None = None
_auth_kwargs: dict[str, Any] = {}

_OWNER_SUBJECTS = frozenset(
    s.strip() for s in settings.owner_subjects.split(",") if s.strip()
)


def _allow_subject(subject: str) -> bool:
    """Authorization predicate for the MCP OAuth layer (M1.1).

    Owner subjects always pass. In single-user mode nobody else does — the old
    allowlist-of-one behavior. In multiuser, an active tenancy account passes;
    everyone else is refused (they must accept an invite on the dashboard first).
    """
    if subject in _OWNER_SUBJECTS:
        return True
    if not settings.multiuser:
        return False
    user = registry.tenancy.user_by_subject(subject)
    return user is not None and user.status == "active"


if _AUTH_ENABLED:
    _oauth_store = InMemoryOAuthStore(path=settings.oauth_store_path or None)
    _idp = get_idp(settings.oauth_provider, settings)
    _provider = ProxyOAuthProvider(
        store=_oauth_store,
        idp=_idp,
        public_url=settings.public_url,
        callback_path=settings.oauth_callback_path,
        allow_subject=_allow_subject,
    )
    _auth_kwargs = {
        "auth_server_provider": _provider,
        "auth": AuthSettings(
            issuer_url=settings.public_url,
            # Same as issuer: this server is both the OAuth AS and the protected
            # resource; enables /.well-known/oauth-protected-resource discovery.
            resource_server_url=settings.public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["mcp"],
        ),
    }
else:
    print(
        "=" * 72
        + "\nENGRAM: OAuth is DISABLED — no client credentials configured for\n"
        f"ENGRAM_OAUTH_PROVIDER={settings.oauth_provider!r}. The /mcp endpoint is\n"
        "UNAUTHENTICATED. Local dev only — do NOT expose this process through\n"
        "the tunnel.\n" + "=" * 72,
        flush=True,
    )


# The server binds locally (127.0.0.1) but is reached via the public tunnel
# hostnames, so DNS-rebinding protection must allow both public Host headers
# (else POST /mcp -> 421).
_public_host = urlparse(settings.public_url).netloc or "localhost"
_explorer_host = urlparse(settings.explorer_url).netloc or "localhost"

# The MCP-native protocol note. Claude Code (and spec-honoring clients) inject
# this at connect; claude.ai web currently DROPS it (anthropics/claude-ai-mcp#131)
# — there, the tool descriptions + the uploadable skill zip carry the protocol.
_MCP_INSTRUCTIONS = """Engram is this user's persistent memory + their team's shared brain.
SESSION START: call kb_load(project) (or kb_projects to list; kb_attach_project to pin).
Surface unread messages FIRST, then confirm state in ONE line. NAVIGATE, NEVER INGEST:
read indexes, then kb_read single files (~5/session). Write decisions/notes the moment
they settle (kb_write); append session summaries with kb_append_log at close.
TEAM: before sinking effort into a hard problem, kb_explore(query=...) — a teammate may
have already solved it; cite them. kb_team() shows who's working. Rooms = live
agent-to-agent conversation: kb_room_post(room, msg, wait_for_reply=True) long-polls
server-side — NEVER poll in a loop. Closing a room OFFERS its outcome; write it to the
brain only after the user says yes. Never write secrets; bodies are scanned.
TAKING TURNS (rooms AND threads, same protocol): name yourself with speaker=/sender=,
and read `floor.do_next` in every result — one sentence saying what to do. It tells
your turn apart from someone genuinely listening, from nobody having joined, from
everyone having left, and from the room being blocked on the PERSON. If it says the
room needs the user, ask them in chat and relay with kb_room_relay_answer — never
send them to a web page. Pass expect_cursor so you learn what landed while you were
composing. After ~4 empty polls it tells you to stop; stop.
The full protocol lives at skills/engram/SKILL.md (kb_read it if unsure) and
library/runbooks/room-turn-taking.md."""

mcp = FastMCP(
    "engram",
    instructions=_MCP_INSTRUCTIONS,
    host=settings.mcp_host,
    port=settings.mcp_port,
    log_level=settings.log_level,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_public_host, _explorer_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[
            settings.public_url.rstrip("/"),
            settings.explorer_url.rstrip("/"),
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    ),
    **_auth_kwargs,
)


# ------------------------------------------------------------------ kb_* tools

# Brain Navigator widget (SEP-1865): when ENGRAM_WIDGET is on, the three
# navigation tools advertise the ui:// resource so a capable host mounts the
# inline card. When off, _nav_meta is None and the tools stay plain.
# v3 unified app: EVERY launcher now mounts ui://engram/app (one widget, five
# tabs). The old per-widget resources stay registered below so a card already
# mounted in an existing chat keeps resolving — only NEW mounts get the app.
_app_meta = app_launcher_meta(settings.widget)
_app_only_meta = app_only_meta(settings.widget)
_nav_meta = _app_meta
_legacy_nav_meta = navigator_tool_meta(settings.widget)  # kept: stale-chat contract

# Meeting widget (SEP-1865): kb_meetings mounts it (model+app visible, stays a
# fully useful plain tool when no widget host is present); the other three are
# APP-ONLY — the widget's own data plane, invisible to the model, zero context
# cost, callable only from the widget's tools/call bridge.
_meet_meta = _app_meta  # v3: meetings mount the unified app's Rooms tab
_meet_app_meta = meetings_app_tool_meta(settings.widget)


@mcp.tool(meta=_nav_meta)
async def kb_projects() -> list[dict[str, Any]]:
    """List all projects in Hiren's knowledge base. Call this when the user asks what
    they're working on, mentions choosing a project, or at the start of a work session
    before any project is identified. Cheap — reads only index files. If no project is
    named yet, ask which one (or infer it from what the user is discussing).

    Returns [{id, title, description, status, last_session, unread_messages}].

    A FRESH brain (no projects yet) returns a single 'welcome' entry — kb_read it and
    run the guided first session it contains WITH the user, step by step.

    When the Navigator widget mounts from this call, say one short line and let the user drive it.
    """
    projects = await (await current_store()).kb_projects()
    if not projects:
        # AI-led onboarding: the connector is the only thing a new user installs;
        # their Claude does the rest. This pseudo-entry is the breadcrumb that
        # works on EVERY client — skill or no skill, claude.ai or Code.
        return [{
            "id": "welcome", "title": "Welcome — run your first session",
            "description": "Fresh brain. kb_read('welcome.md') and walk the user "
                           "through the 6-step first session it contains.",
            "status": "active", "folder": "", "unread_messages": 0, "last_session": None,
        }]
    return projects


@mcp.tool(meta=_nav_meta)
async def kb_load(project: str, lite: bool = False) -> dict[str, Any]:
    """Load a project's working context. Call when the user names a project to work on
    ('load alt', "let's do hyprlocl work"). Returns state + navigation indexes + unread
    inter-session messages — NOT concept bodies; fetch those individually with kb_read
    as needed (navigate, never ingest: a good session touches ~5 files).

    Pass lite=True when RESUMING a project you already know this session (or just loaded) —
    a token-thrifty view that drops the index_tree and every message body, returning only
    context_md, the last log entry, an unread COUNT + message titles, active_concepts, and
    the server manifest. Use the full load (lite=False, the default) only on the FIRST touch
    of a project, when you actually need the navigation tree and message bodies. Re-loading
    full when you already have the shape just re-buys those tokens.

    Surface unread messages to the user FIRST — they are instructions from a previous
    session, possibly addressed to a surface via their `to` field: 'claude-code' means
    Claude Code sessions, 'mobile' and 'web' mean claude.ai; 'any' means whoever loads
    next. Act on them (or ask), then call kb_mark_read. Messages whose `expired` flag
    is true: mention briefly, archive, don't act. Then confirm project state in ONE
    line (current phase + top open loop) — do not recite the whole context back.

    Every project has three anchors (context.md, log.md, messages/) — beyond that,
    structure varies by project; the index_tree shows this project's actual shape.
    Before inventing new structure, kb_read 'library/runbooks/organizing-projects.md'
    for the house conventions.

    Returns {project, context_md, index_tree, recent_log (last 3 entries),
    unread_messages (full bodies), active_concepts (frontmatter only), server}.

    The `server` block lists the tools this server currently offers. If any tool it
    names is NOT in your available tools, this chat predates the latest update: tell the
    user their chat is running a stale tool list and to start a FRESH chat to use the
    newer tools (writes here are still safe). Do this only when there's an actual gap.

    When the Navigator widget mounts from this call, say one short line and let the user drive it.

    In multi-user mode the result also carries a `social` block {unread_dms, unread_notifications}
    — surface these too when non-zero (the user has new DMs / notifications waiting;
    kb_notifications and kb_messages show them).
    """
    result = await (await current_store()).kb_load(project, lite)
    _presence_project(project)
    if settings.multiuser:
        user = current_user()
        if user is not None:
            counts = registry.social.unread_counts(user.id)
            result["social"] = {
                "unread_dms": counts.get("dms", 0),
                "unread_notifications": counts.get("notifications", 0),
                # Someone is waiting on an answer about your public work (kb_asks).
                "open_questions": registry.discovery.ask_counts(user.id).get("open_for_me", 0),
            }
    return result


@mcp.tool()
async def kb_read(path: str, depth: int = 0) -> dict[str, Any]:
    """Read one concept file from the KB. Use paths discovered via kb_load's index_tree
    or kb_search — never guess paths. Use depth=1 when you need to know what a
    concept's neighbors are before deciding to read them: it adds the frontmatter of
    every concept the file links to (one hop; dangling links come back missing: true)
    AND backlinks — every concept that cites this one — so the graph walks both ways.

    The result carries `hash` (sha256 of the content). For a safe concurrent edit, pass it
    back as kb_write's `base_hash` so a conflicting write between your read and write is
    rejected instead of silently lost.

    Returns {path, content, meta, hash} plus links + backlinks when depth=1.
    """
    return await (await current_store()).kb_read(path, depth)


@mcp.tool()
async def kb_write(
    path: str,
    content: str,
    message: str,
    description: str = "",
    base_hash: str = "",
    session: str = "",
) -> dict[str, Any]:
    """Create or update a concept. Call IMMEDIATELY when something durable is settled
    in conversation — a decision, spec, runbook, person note — don't batch to session
    end. If the user corrects stored knowledge mid-session, update the concept then and
    there. Content must be OKF: YAML frontmatter with `type` (project, client, person,
    decision, spec, runbook, idea, meeting, video, snippet, reference — or a new type
    if none fit), then a markdown body. Link related concepts with relative markdown
    links, never wikilinks. Always include relative markdown links to related concepts —
    the decision a spec implements, the spec a decision shaped, the concept this
    supersedes. A concept with no links is a dead end for depth=1 navigation. There is
    no fixed folder set — only context.md, log.md, and messages/ are anchors; shape the
    project to the work (research: sources/, experiments/; brainstorm: ideas/; client:
    meetings/) and new directories auto-index. The server
    auto-fills title/description/timestamp (pass
    `description` if the frontmatter lacks one) and on create auto-appends the concept
    to its parent index.md. Filenames kebab-case; decisions as YYYY-MM-slug.md. Paths
    are repo-relative POSIX, e.g. 'projects/alt/decisions/2026-07-search-engine.md'.
    Reserved: index.md and log.md are unwritable here (indexes are server-maintained;
    use kb_append_log for the log), and messages/ only via kb_leave_message.
    context.md IS writable — session close updates it. HTML artifacts built in chat
    (side-panel documents) are saved VERBATIM: frontmatter with type: artifact,
    format: html, and sources, then the COMPLETE HTML document as the body — the
    share link then serves the real interactive page, and updating an artifact never
    loses its existing share token. To mark that this concept REPLACES older ones, add a
    `supersedes:` frontmatter field (a repo-relative path, or a list of them): the server
    validates each target exists, stamps it superseded (confidence: superseded,
    superseded_by pointing back here, valid_until: today) in the SAME commit, and makes the
    edge walkable from kb_read depth=1 — never leave a superseded decision looking current.
    `message` is the git commit
    message. If the write fails on a conflict, re-read the file, merge intent
    manually, and retry — never overwrite blind. For safe concurrent edits in a
    multi-session workspace, pass kb_read's returned `hash` as `base_hash`: the write is
    then REJECTED (nothing overwritten) if the file changed on disk since you read it, so a
    conflicting concurrent write can't be silently lost — re-read, merge, retry. Pass
    `session` (this session's id) so a foreign kb_claim on the path surfaces a heads-up
    warning that you may be colliding.

    Returns {path, created, no_change, sha, pushed, warnings, indexes_updated, superseded}.
    """
    # Check BEFORE recording our own write, so the question is "was anyone else
    # here" rather than "was I".
    clobber = _clobber_warning(path, base_hash)
    _note_activity(path)
    res = await (await current_store()).kb_write(
        path, content, message, description, base_hash, session
    )
    if clobber and not res.get("no_change"):
        res.setdefault("warnings", []).append(clobber)
    return res


@mcp.tool()
async def kb_edit(
    path: str,
    operation: str,
    content: str = "",
    find: str | None = None,
    section: str | None = None,
    occurrence: int | str = 1,
    session: str = "",
) -> dict[str, Any]:
    """Surgically edit part of a concept without rewriting the whole file — append/prepend/
    find_replace/replace_section/insert; body only, use kb_write for frontmatter or new
    concepts. Reach for this over kb_write when you want to change ONE part of an existing
    concept and leave the rest byte-for-byte: adding a bullet, fixing a line, swapping a
    version string, replacing a section. Operations:
    - append: add `content` to the end of the body.
    - prepend: add `content` right after the frontmatter, before the body.
    - find_replace: replace `find` with `content`; `occurrence` picks which — 1 (default)
      = first match, an integer N = the Nth match, or "all" = every match. A zero-match
      `find` is an error (the anchor must exist; matching is literal, not fuzzy).
    - replace_section: replace the block under the markdown heading named by `section`
      (e.g. "## Notes"); the heading stays, the lines under it become `content`.
    - insert_after / insert_before: place `content` just after/before the body line that
      contains the `find` anchor.
    The frontmatter fence is never touched — an edit whose anchor lives in the frontmatter
    is refused with a pointer to kb_write. index.md, log.md and messages/ are not editable
    here (same rules as kb_write). The concept must already exist; create new ones with
    kb_write. Pass `session` (this session's id) so a foreign kb_claim on the path surfaces
    a heads-up warning that you may be colliding.

    Returns {path, sha, pushed, operation, warnings}.
    """
    # kb_edit has no base_hash: it is anchored, so a conflicting concurrent change
    # usually breaks the anchor and refuses loudly. It can still land on content
    # that moved under it, which is worth flagging.
    clobber = _clobber_warning(path)
    _note_activity(path)
    res = await (await current_store()).kb_edit(
        path, operation, content, find, section, occurrence, session
    )
    if clobber:
        res.setdefault("warnings", []).append(clobber)
    return res


@mcp.tool()
async def kb_move(old_path: str, new_path: str) -> dict[str, Any]:
    """Rename/relocate a single concept, rewriting every link to it bundle-wide — use
    kb_rename_project for whole projects. Reach for this when the user wants ONE file to
    live somewhere else or be named differently ('move that decision under specs/',
    'rename this concept'), or when reorganizing a project's shape. Moves one concept file
    from old_path to new_path
    (both repo-relative POSIX .md paths; new_path must be free) and keeps the graph intact:
    it re-bases the moved file's own relative links, rewrites every relative markdown link
    ANYWHERE in the bundle that pointed at it, updates both the old and new parent index.md,
    and fixes any frontmatter sources/supersedes/superseded_by references to it — all in one
    commit. index.md, log.md and messages/ cannot be moved.

    Returns {old, new, links_rewritten, sha, pushed}.
    """
    return await (await current_store()).kb_move(old_path, new_path)


@mcp.tool()
async def kb_append_log(
    project: str, entry: str, commits: list[str] | None = None, repo: str = ""
) -> dict[str, Any]:
    """Append a dated entry to the project's session log (prepends under the H1,
    newest first; history is never edited). This is the session-close tool — when the
    user says 'close session' or the work clearly wraps up, run the checklist:
    (1) DRAFT the log entry — date, what happened, decisions made with links to new
    concepts, open threads — and SHOW it to the user BEFORE calling this tool;
    (2) update the project's context.md (Current Phase / Open Loops / Next Actions)
    via kb_write; (3) offer kb_leave_message for anything the next session must be
    told directly; (4) confirm in one line what was committed. Offer the close-out
    proactively — don't let sessions end with unwritten state. Entries are
    auto-formatted as scannable bullets ('* **title** — body') under a bare ISO date
    heading, so pass a first line that works as a title with the detail after it.

    PROOF, not assertion: when the session shipped code, pass commits=['<sha>', ...]
    with repo='owner/name' (or full commit URLs, or 'owner/name@sha'). The entry then
    records clickable GitHub links, so a future session reading "shipped X" can verify
    it in one click instead of taking a past session's word for it. An entry that claims
    shipping with no proof comes back with a warning saying so.

    Returns {ok, path, sha, date, pushed, warnings}.
    """
    return await (await current_store()).kb_append_log(project, entry, commits, repo)


@mcp.tool()
async def kb_leave_message(
    project: str,
    title: str,
    body: str,
    to: str = "any",
    priority: str = "normal",
    expires: str | None = None,
) -> dict[str, Any]:
    """Leave an explicit instruction for a FUTURE session ('verify DNS Tuesday before
    CMS work'). Messages are not logs (history) and not concepts (durable knowledge) —
    they are instructions with a lifecycle: the next session that loads the project
    sees them, acts, and archives them with kb_mark_read. Address `to` a surface when
    it matters: 'claude-code' for Claude Code sessions, 'mobile' or 'web' for
    claude.ai, 'any' (default) for whoever loads next. Set priority 'high' for
    must-see items and expires (YYYY-MM-DD) for instructions that go stale.

    Returns {path, sha, pushed, warnings}.
    """
    return await (await current_store()).kb_leave_message(project, title, body, to, priority, expires)


@mcp.tool()
async def kb_mark_read(message_path: str) -> dict[str, Any]:
    """Archive an inter-session message AFTER acting on it: flips its status to read
    and moves it to messages/archive/. Pass the exact path from kb_load's
    unread_messages. Expired messages get archived too (mention them briefly, don't
    act on them).

    Returns {archived_path, sha, pushed}.
    """
    return await (await current_store()).kb_mark_read(message_path)


@mcp.tool(meta=_nav_meta)
async def kb_search(
    query: str,
    project: str | None = None,
    type: str | None = None,  # noqa: A002 — tool contract field name
    limit: int = 8,
    expand: bool = True,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Search the whole knowledge base for concepts by meaning and text. Runs the
    semantic (vector) engine and the pure-text scorer and FUSES them: when both have
    hits the results are combined by reciprocal-rank fusion and each carries
    engine='hybrid'; when only one engine is available its results serve alone with
    engine='semantic' or 'text'. Use it to find concepts whose paths you don't already
    have — including in projects not currently loaded, and in self/ (Hiren's stack and
    preferences) and library/ (cross-project runbooks): search there before reinventing
    a procedure that likely already exists.

    Multi-query expansion is ON by default (expand): the query is broadened into a few
    deterministic offline variants (keyword-only, light synonyms) so a concept phrased
    differently than your query is still found; pass expand=false for a literal search.
    Optional filters: project (project id) and type (frontmatter type, e.g. 'decision',
    'runbook'). Optional time window: since/until (ISO dates, either alone) ALSO pulls in
    every concept whose frontmatter timestamp (or log entry date) falls in the window
    regardless of relevance — those rows are flagged window=true — for 'what did we
    decide about X back in June' recall. Results are ranked best-first; follow up with
    kb_read on the paths.

    Returns [{path, title, description, score, matched_heading, engine, window?}] where
    engine is 'hybrid' | 'semantic' | 'text' and window (present only on time-window
    hits) is true.

    When the Navigator widget mounts from this call, say one short line and let the user drive it.
    """
    return await (await current_store()).kb_search(
        query, project=project, type=type, limit=limit, expand=expand, since=since, until=until
    )


@mcp.tool()
async def kb_inbox(text: str) -> dict[str, Any]:
    """Quick-capture a thought/task/link into the brain's inbox with ZERO ceremony —
    call whenever the user says 'remember this', 'note this down', 'inbox: ...'. No
    project, no frontmatter decisions, no links required: it drops the raw text into
    inbox/YYYY-MM-DD-<slug>.md at the bundle root as an untriaged item and pushes.
    Triage later moves it to a proper project concept (kb_write) — the morning briefing
    lists everything still untriaged so nothing captured gets lost.

    Returns {path, sha, pushed}.
    """
    return await (await current_store()).kb_inbox(text)


@mcp.tool()
async def kb_thread_post(
    thread: str,
    sender: str,
    message: str,
    close: bool = False,
    topic: str = "",
    refs: list[str] | None = None,
    allow_secrets: bool = False,
    wait_for_reply: bool = False,
    wait_seconds: int = 25,
    hand_to: str = "",
    ask_human: str = "",
    expect_cursor: str = "",
    goal: str = "",
    exit_condition: str = "",
) -> dict[str, Any]:
    """Send a message to ANOTHER Claude session over a shared, named thread — agent-to-agent
    async chat, NO project needed. Call this when a session should talk to a different
    session: the user says 'start a thread called X and ask the other session ...' or 'join
    thread X and reply'. The thread is auto-created on the first post (topic set from the
    `topic` arg then); later posts just append. `thread` is a kebab-case id (e.g.
    'deploy-handoff'); `sender` is this session's name (e.g. 'session-a') so the other side
    knows who spoke.

    PREFER wait_for_reply=True (with wait_seconds=25) over posting then polling — it makes
    ONE tool call = post + await the answer: the server holds the call open and long-polls
    for the first turn from a DIFFERENT sender (or a close), returning it as `reply` (or
    `reply: null` + `waited: true` on timeout). Every separate kb_thread_read poll costs the
    user tokens (each result lands in the conversation); long-poll is FREE — it sleeps
    server-side. Loop this one call to hold a whole conversation at ~1/10th the tool calls.

    Need input from Hiren himself? Post a turn addressed '@hiren: <question>' and
    wait_for_reply=True — he sees open threads live in /brain/office and can reply from the
    browser (his turn arrives as your `reply`, costing zero polling tokens).

    Share concepts/artifacts INTO the room with `refs` — a list of repo-relative paths (e.g.
    ['projects/alt/specs/api.md']); they attach to the turn and show as a 'shared:' line the
    other session can kb_read. To share a CODE snippet, drop a fenced ``` code block ``` into
    `message` — it renders verbatim in the transcript, no special param needed.

    Set close=True to END the thread — that is the agreed stop signal; the turn you post is
    the final one and further posts are refused. If you are NOT using wait_for_reply, poll
    with kb_thread_read(since=cursor, wait_seconds=25) — its own long-poll is likewise free,
    far cheaper than tight 2-3s polling. The user can run /loop to drive it hands-free.

    The message is scanned for likely secrets before it commits — threads are private but
    written to GIT HISTORY permanently — and refused if any are found (pass allow_secrets=True
    to override a placeholder). A `refs` path that doesn't exist is warned about, not blocked.

    OPEN WITH INTENT. On the first post, set `goal` (what this thread is for) and
    `exit_condition` (how you will know it is finished). Both are set once and never
    rewritten — a goal you can edit mid-conversation becomes whatever the conversation
    drifted into. They cost one sentence and they are what makes an agent conversation
    terminate: the turn-cap nudge quotes them back, so "40 turns in" becomes "40 turns
    in, and the exit condition was X — is it met?", which is a question with an answer.

    TAKING TURNS — threads carry the same floor as rooms. `sender` is your name, and
    posting hands the floor on: to the other party with two, to whoever has spoken least
    recently with more. Read `floor.do_next` in the result — one sentence saying what to
    do. It distinguishes the four states agents otherwise conflate into one hopeful
    guess: your turn / someone is genuinely listening / nobody ever joined / they were
    here and have gone. `hand_to` names a specific next speaker.

    WHEN ONLY HIREN CAN DECIDE, pass `ask_human` with the question instead of posting
    '@hiren:' and hoping. It blocks the thread on him, takes the floor from every agent
    so nobody waits on a session that is itself waiting, and notifies him. Whichever
    session is talking to him relays his answer with kb_room_relay_answer.

    PASS `expect_cursor` — the cursor from the read you are replying to. On threads that
    is an ISO timestamp string, not the integer a room uses; the cursors are of the
    thread's own type on each surface. Anything that landed while you were composing
    comes back in `missed`. Your post always goes through.

    Returns {thread, seq, status, participants, posted, pushed, warnings, floor,
    server_ms} plus {reply, waited?} when wait_for_reply=True. `server_ms` is the
    SERVER's own elapsed time — the only honest way to measure a wait, since bracketing
    a tool call with `date` also measures your own inference.
    """
    _rate_limit_post()
    started = time.monotonic()
    key = _speaker_key()
    fid = _thread_floor_id(thread)
    store = await current_store()

    # What landed while you were composing, captured before our own write.
    missed: list[dict[str, Any]] = []
    if expect_cursor.strip():
        try:
            prior = await store.kb_thread_read(thread, expect_cursor.strip(), 0)
            missed = list(prior.get("turns") or [])
        except Exception:  # noqa: BLE001 — a bad cursor must not block a post
            missed = []

    # ALL coordination state settles BEFORE the transcript write, because the
    # write is what wakes everyone parked on this thread. Updating the floor
    # afterwards let a waiter observe the turn with the PREVIOUS floor still in
    # place — seen live: mac-a posted, and a parked session read `last_spoke: ""`
    # and the floor unmoved. Rooms never had this because post_turn moves the
    # floor inside the same transaction that writes the turn.
    if fid is not None:
        me = _require_room_user()
        if key:
            registry.rooms.touch_speaker(fid, key, me.id, name=sender)
            registry.rooms.clear_empty_waits(fid, key)
            registry.rooms.record_speech(
                fid, key, hand_to=(hand_to or None) if hand_to else None,
            )
        if ask_human.strip():
            registry.rooms.ask_human(fid, ask_human, asker=key)
            _push_notification(
                me.id, "room_question",
                f"Thread '{thread}' needs you: {ask_human.strip()[:160]}",
                ref=thread,
            )

    out = await store.kb_thread_post(
        thread, sender, message, close, topic, goal, exit_condition, refs,
        allow_secrets, wait_for_reply, wait_seconds,
    )
    if fid is not None:
        out["floor"] = await _with_working(registry.rooms.floor_state(fid, key))
        # THE DELIVERED VERDICT, on threads too. A session that has gone cannot be
        # woken by any amount of waiting, so the person's tray is the only route
        # left. This existed on rooms only — which meant the surface a single user
        # with several machines will actually use was the one that stayed silent.
        # Threshold is GONE, not "not parked right now", and one ping per person.
        gone = [o for o in out["floor"]["others"]
                if not o["live"] and not o["listening"]]
        for uid in dict.fromkeys(int(o["user_id"]) for o in gone):
            asleep = [o["name"] for o in gone if int(o["user_id"]) == uid]
            _push_notification(
                uid, "room_turn",
                f"Thread '{thread}' — a turn is waiting for {', '.join(asleep)}: "
                f"{message[:120]}",
                ref=thread,
            )
    if missed:
        out["missed"] = missed
        out.setdefault("warnings", []).append(
            f"{len(missed)} turn(s) landed while you were composing — they are in "
            "`missed`. Read them before acting on what you just said."
        )
    # TERMINATION. Rooms carry a turn budget precisely so agent conversations end
    # instead of agreeing politely forever; threads had only a per-minute rate
    # limit, which stops a burst but not a runaway. Now that threads run the same
    # protocol for agent-to-agent work, they need the same forcing function.
    # A warning rather than a refusal: a thread is a permanent record with no
    # goal or exit condition to fall back on, so cutting one off mid-exchange
    # could strand a conversation with no defined way to resume it. The nudge
    # escalates instead, and the rest ladder already handles runaway WAITING.
    seq = int(out.get("seq") or 0)
    # Quote what the thread is FOR. "40 turns in" is a scold; "40 turns in, and
    # the exit condition was X" is a question the reader can actually answer —
    # which is the whole reason rooms carry a goal and threads now do too.
    aim = ""
    if out.get("exit_condition"):
        aim = f" The exit condition was: {out['exit_condition']!r}. Is it met?"
    elif out.get("goal"):
        aim = f" The goal was: {out['goal']!r}. Is it done?"
    if seq >= _THREAD_TURN_CAP:
        out.setdefault("warnings", []).append(
            f"This thread is {seq} turns long, past the {_THREAD_TURN_CAP}-turn cap. "
            "Stop and close it (close=True) with what was decided. A conversation "
            "this long has either finished or lost its way, and every further turn "
            "costs each member the whole history again." + aim
        )
    elif seq >= _THREAD_TURN_BUDGET:
        out.setdefault("warnings", []).append(
            f"{seq} turns in. If the point is settled, say so and close the thread "
            "(close=True) rather than posting another agreeable turn — agent "
            "conversations end because someone ends them." + aim
        )
    if close:
        # CLOSING SHOULD PRECIPITATE. A room drafts its outcome on close; a thread
        # just stopped, leaving a transcript nobody distils. The transcript is in
        # git either way, but a transcript is not knowledge — the next session has
        # to re-read the whole exchange to learn what was decided. Offered, never
        # written: a conclusion belongs to the user, same rule as rooms.
        out["precipitate_instruction"] = (
            "This thread is closed. Write 3-10 lines of what was actually decided "
            "or learned — not a summary of the conversation, the part a session "
            "reading it in three months needs. Offer that to the user, and only "
            "on their explicit yes save it with kb_write (type decision or note), "
            f"ending the body 'From thread {thread}, closed <date>'. Never write "
            "it unasked."
        )
    out["server_ms"] = int((time.monotonic() - started) * 1000)
    return out


@mcp.tool()
async def kb_thread_read(
    thread: str, since: str | None = None, wait_seconds: int = 0, sender: str = ""
) -> dict[str, Any]:
    """Read a cross-session thread for new turns — the WAIT half of agent-to-agent chat.
    PREFER wait_seconds=25 over tight polling: it long-polls SERVER-SIDE and returns the
    instant a new turn from another sender arrives (or the thread closes), or empty at
    timeout. One waiting call replaces a whole loop of reads — and each separate poll costs
    the user tokens (every result lands in the conversation), while the long-poll wait is
    FREE (it sleeps on the server). Loop this one call to keep waiting.

    ALWAYS pass the previous read's `cursor` as `since` — it returns only turns AFTER that
    point. Re-reading with no cursor re-buys every old turn in tokens on every poll. The
    response's `cursor` is the newest turn's timestamp; feed it back as the next `since`.
    A thread that was never created returns {status: 'none', turns: []} (not an error — a
    joiner may read before the opener posts), and long-poll keeps waiting through it.

    Pass your `sender` name — reading REGISTERS you as present, so the others can see
    there is somebody to talk to, and while you long-poll you show as listening rather
    than as someone who might have left. Without it you are invisible until you speak,
    and a reconnect makes you a stranger.

    Returns {thread, status, topic, participants, turns: [{seq, sender, timestamp,
    message}], cursor, closed_by?, floor} — check `floor.do_next`.
    """
    started = time.monotonic()
    key = _speaker_key()
    fid = _thread_floor_id(thread)
    if fid is not None and key:
        registry.rooms.touch_speaker(
            fid, key, _require_room_user().id, name=sender,
            listening_seconds=(
                _wait_window(wait_seconds) + _LISTEN_GRACE_S if wait_seconds > 0 else 0
            ),
        )
    out = await (await current_store()).kb_thread_read(thread, since, wait_seconds)
    if fid is not None:
        if key and wait_seconds > 0:
            if out.get("turns"):
                registry.rooms.stop_listening(fid, key)
                registry.rooms.clear_empty_waits(fid, key)
            else:
                registry.rooms.note_empty_wait(fid, key)
        elif out.get("turns") and key:
            registry.rooms.stop_listening(fid, key)
        out["floor"] = await _with_working(registry.rooms.floor_state(fid, key))
    # The server's own elapsed time. Absent here, any timing report a session makes
    # is really its inference latency — the error that produced a phantom backlog
    # bug this morning, from two sessions, using the same broken instrument.
    out["server_ms"] = int((time.monotonic() - started) * 1000)
    return out


@mcp.tool()
async def kb_threads() -> list[dict[str, Any]]:
    """List active cross-session threads so a session can discover one to join by name, or
    check what's open. Reach for this when the user says 'join the thread the other session
    started' without giving its id, or 'what threads are open'. Newest activity first.

    Returns [{thread, status, topic, participants, turn_count, last_activity}].
    """
    return await (await current_store()).kb_threads()


@mcp.tool(meta=_meet_meta)
async def kb_meetings() -> dict[str, Any]:
    """Show Hiren his live meeting rooms — call when he asks about meetings, threads,
    what agents are discussing, or wants to reply to one from claude.ai (especially
    mobile, where the browser office isn't handy). Lists only OPEN cross-session
    threads with a preview of the last turn; `needs_hiren` flags a room whose last
    turn starts '@hiren:' waiting on a reply.

    Returns {threads: [{thread, topic, status, participants, turn_count,
    last_activity, last_turn: {sender, message, timestamp} | null, needs_hiren}]}.

    Mounts the unified Engram app on its Rooms tab; keep your text to one short line
    after it mounts.
    """
    payload = await meetings_payload(await current_store())
    return {"view": "rooms", **payload}  # unified app: opens the Rooms tab


@mcp.tool(meta=_meet_app_meta)
async def meetings_state() -> dict[str, Any]:
    """App-only poll target for the meetings widget's rooms list — same shape as
    kb_meetings. Never call this yourself; it exists for the widget's own bridge.

    Returns {threads: [...]} (see kb_meetings).
    """
    return await meetings_payload(await current_store())


@mcp.tool(meta=_meet_app_meta)
async def meeting_transcript(thread: str, since: str = "") -> dict[str, Any]:
    """App-only poll target for the meetings widget's transcript view — a thin
    wrapper over kb_thread_read. Never call this yourself; it exists for the
    widget's own bridge.

    Returns {thread, status, topic, participants, turns: [{seq, sender, timestamp,
    message}], cursor, closed_by?}.
    """
    return await (await current_store()).kb_thread_read(thread, since=since or None, wait_seconds=0)


@mcp.tool(meta=_meet_app_meta)
async def meeting_reply(thread: str, message: str) -> dict[str, Any]:
    """App-only: post `message` into an OPEN thread as Hiren (sender is always
    'hiren' — the OAuth allowlist means every caller through this server IS him).
    Reply-only, mirroring the web office exactly: the thread must already exist
    and be open (never creates or reopens one), 4000-char cap, secret-scanned like
    any other thread post. Never call this yourself; it exists for the widget's
    own bridge.

    Returns {thread, seq, status, participants, posted, pushed, warnings}.
    """
    return await _meeting_reply_impl(await current_store(), thread, message)


@mcp.tool()
async def kb_presence(
    session: str,
    name: str = "",
    status: str = "working",
    working_on: str = "",
    repo: str = "",
    branch: str = "",
    repo_remote: str = "",
    cwd: str = "",
    project: str = "",
    note: str = "",
    host: str = "",
) -> dict[str, Any]:
    """Announce/heartbeat THIS session so other sessions (and Hiren's dashboard) can see
    who's working on what. Call at session start and again whenever your task changes — one
    file per session, overwritten each time (no history). A Claude Code session should first
    auto-detect its git context and pass real values: repo = basename of
    `git rev-parse --show-toplevel`, branch = `git rev-parse --abbrev-ref HEAD`,
    repo_remote = `git remote get-url origin` (or 'local' if none), cwd = the working dir,
    host = the PC name (`hostname`). A claude.ai session self-reports what the user says.
    `session` is a short kebab-case handle for this session (e.g. 'pc1-claude-code');
    `status` is one of working | idle | blocked | available | done. `note` is an optional
    freeform line. The user can run /loop to heartbeat hands-free.

    Returns {session, updated, roster_active} where roster_active = sessions active in the
    last 15 minutes (including this one).
    """
    return await (await current_store()).kb_presence(
        session, name, status, working_on, repo, branch, repo_remote, cwd, project, note, host
    )


@mcp.tool()
async def kb_roster(active_within_min: int = 15) -> list[dict[str, Any]]:
    """See which Claude sessions are currently active across all of Hiren's PCs and
    projects, and what each is working on (repo/branch/task) — the 'who's online' board.
    Call when the user asks what's running, before handing off work, or to coordinate.
    Sessions that haven't heartbeat within `active_within_min` (default 15) are filtered
    out (their records are kept, not deleted); pass active_within_min<=0 to disable the
    filter and see every recorded session. Most-recently-updated first.

    Returns [{session, name, status, working_on, repo, branch, repo_remote, cwd, project,
    host, updated, age_min}].
    """
    return await (await current_store()).kb_roster(active_within_min)


@mcp.tool()
async def kb_handoff(
    from_session: str,
    summary: str,
    repo: str = "",
    branch: str = "",
    state: str = "",
    next_steps: str = "",
    refs: list[str] | None = None,
    to: str = "any",
    room: str = "",
    allow_secrets: bool = False,
) -> dict[str, Any]:
    """Hand your current work to another session (or leave it for whoever picks it up):
    captures repo / branch / state / next-steps / refs so they resume exactly where you left
    off. Use when the user says 'hand this off', 'another session will continue', or when
    wrapping a session that has unfinished work. `from_session` is your session name;
    `summary` is what's being handed off; `next_steps` is the to-do for the taker; `refs` is
    a list of repo-relative concept/artifact paths they'll need; `to` names an intended taker
    (default 'any'). Pass `room` (a kebab-case thread id) to ALSO drop a pointer into that
    room so a watching session is notified immediately.

    summary and next_steps are scanned for likely secrets before the record commits —
    handoffs are private but written to GIT HISTORY permanently — and refused if any are found
    (pass allow_secrets=True to override a placeholder). `notified` reports whether the room
    pointer posted; `room_error` carries the reason when it didn't.

    Returns {path, sha, pushed, notified, room_error?}.
    """
    return await (await current_store()).kb_handoff(
        from_session, summary, repo, branch, state, next_steps, refs, to, room, allow_secrets
    )


@mcp.tool()
async def kb_workspace() -> dict[str, Any]:
    """One-shot snapshot of the whole workspace — who's active, what rooms are open, and
    recent handoffs. Use to brief the user on everything happening across their sessions
    ('what's going on across my machines', 'give me the workspace board'). Combines the live
    roster (active 15 min), open threads, and the last ~5 handoffs in a single call.

    Returns {roster: [...same as kb_roster...], rooms: [...same as kb_threads...],
    recent_handoffs: [{path, from, to, summary, repo, branch, state, next_steps, refs,
    created, status}]}.
    """
    return await (await current_store()).kb_workspace()


@mcp.tool()
async def kb_claim(session: str, path: str, note: str = "") -> dict[str, Any]:
    """Claim a concept/file/task in a multi-session workspace so OTHER sessions know you're
    working on it and don't clobber you — an ADVISORY lock (it never blocks anyone). Call it
    BEFORE you start editing a file or concept when more than one session is live; release it
    when done. `session` is your session id; `path` is the repo-relative concept path you're
    editing (e.g. 'projects/alt/specs/api.md') or a short free-text task string. One record
    per path, overwritten on re-claim. If a DIFFERENT session already holds a live claim on it,
    this still succeeds but returns already_claimed_by so you can warn the user and coordinate
    (kb_thread_post) rather than collide. Claims older than 30 min are treated as stale.

    Returns {path, session, claimed_at, already_claimed_by?}.
    """
    return await (await current_store()).kb_claim(session, path, note)


@mcp.tool()
async def kb_release(session: str, path: str) -> dict[str, Any]:
    """Release your advisory claim on a path once you're done editing it, so other sessions
    know it's free. Only removes the claim if YOUR session holds it (otherwise a no-op that
    reports who does). `session` is your session id; `path` is the same path you claimed.

    Returns {path, released, note?}.
    """
    return await (await current_store()).kb_release(session, path)


@mcp.tool()
async def kb_claims() -> list[dict[str, Any]]:
    """List the current advisory claims across all sessions — what each session has flagged
    as theirs to work on. Use before starting on a file, or to see if a path you want is
    already claimed. Active (claimed within 30 min) first, then stale.

    Returns [{path, session, note, claimed_at, age_min, stale}].
    """
    return await (await current_store()).kb_claims()


@mcp.tool()
async def kb_import(source: str, payload: str, dry_run: bool = True) -> dict[str, Any]:
    """Backfill the brain from a ChatGPT or Claude data export — paste/point at the
    exported conversations JSON. `source` is 'chatgpt' or 'claude'. Defaults to a DRY
    RUN: it parses the export and returns what WOULD be imported (one proposal per
    conversation) WITHOUT writing anything, so you can show the user the set first. Call
    again with dry_run=false to actually file them into inbox/imports/ as
    type: imported-conversation concepts (paths that already exist are skipped) for later
    triage into proper concepts. SIZE CAVEAT: exports can be large — for a big history,
    summarize what you'd import and confirm before a non-dry-run rather than dumping
    everything; each conversation body is capped and marked truncated when it overflows.

    Returns {source, proposed: [{path, title, timestamp, message_count, truncated}],
    imported: [paths], skipped: [paths]}.
    """
    return await (await current_store()).kb_import(source, payload, dry_run)


@mcp.tool()
async def kb_doctor() -> dict[str, Any]:
    """Run a round-trip HEALTH self-test of this brain deployment and report pass/warn/fail.
    Use when the user asks "is my brain healthy?", suspects the server is misconfigured,
    or after a deploy. Non-mutating: checks the git checkout answers, projects read, the
    OKF write/read/delete pipeline is sound (in a throwaway temp dir — no commit), the
    semantic backend is reachable or cleanly text-only, the OAuth store loads, the
    scheduler config is sane, and reports counts (concepts, artifacts, unread, orphans).
    This is the plumbing check; the nightly reconcile's brain-health report is the content
    audit — mention that for orphan/dead-knowledge detail.

    Returns {status, checks: [{name, status, detail}], counts, head}.
    """
    s = await current_store()
    # Each store carries its own settings (per-user brain_path for tenants), so
    # the doctor round-trips the CALLER's brain, not the operator's.
    return await run_doctor(s.settings, s)


@mcp.tool()
async def kb_rename_project(old_id: str, new_id: str) -> dict[str, Any]:
    """Rename a project's id (its folder under projects/). Use when the user says a
    project should be called something else ('rename mcp-explorations to mcp-apps').
    Safe and atomic: moves the folder, rewrites every markdown link across the whole
    knowledge base that pointed at the old id (including from other projects, self/,
    and library/), and updates the denormalized project field inside the moved tree —
    one commit. New id must be kebab-case and unused. metalfinger cannot be renamed.
    After renaming, refer to the project by its new id (kb_load, kb_search filters).
    For a SINGLE concept (not a whole project), use kb_move instead.

    Returns {old, new, links_rewritten, sha, pushed}.
    """
    return await (await current_store()).kb_rename_project(old_id, new_id)


@mcp.tool()
async def kb_realign(
    project: str = "", repo: str = "", cwd: str = "", pin: str = ""
) -> dict[str, Any]:
    """ORIENT THIS SESSION. Call this the moment the user says "realign" (or "realign on
    <project>", "where are we", "what should I be working on") — and at the start of any
    session that doesn't already know which project it belongs to.

    One call replaces four: it resolves WHICH project this is, loads it, surfaces unread
    inter-session messages, and hands you the pin. Resolution order — first answer wins:
      1. `project` if the user named one (loose match: 'vibechk' finds vibechk-brand).
      2. `pin` — the contents of `.engram-project` at the repo root. READ THAT FILE
         YOURSELF and pass it: a pin is a DECLARATION someone deliberately wrote, and it
         commits with the repo, so it resolves correctly on a machine with no history at
         all. It outranks any inference below.
      3. The LEARNED ROUTING TABLE — pass `repo` (git remote or repo name) and `cwd` and
         it maps them to a project from presence history. This table maintains itself:
         every session that attaches to a project from a repo teaches it that route.
      4. A guess from the directory name (returned with guess=true — confirm it).
      5. Nothing matched → resolved=false plus a scored shortlist; ask ONE question naming
         your best candidate. Never make the user read the whole list, and if the work has
         no project yet say so and offer kb_attach_project rather than filing it under a
         neighbour. The `routing` block says whether the table is empty (hooks not
         installed on this machine) or simply had no match — a real difference.

    ALWAYS pass pin, repo and cwd when you can see them (Claude Code: read
    `.engram-project`, your working directory, and `git remote get-url origin`). The pin
    makes resolution exact; repo/cwd make it reliable and teach the table for next time.

    If `pin_nudge` is non-empty, ACT ON IT: write `pin_content` into `.engram-project`
    at the repo root. A pin is per-repo and is the ONLY thing that teaches the routing
    table (presence records take their project from it), so an unpinned repo stays
    invisible to routing no matter how much work happens in it.

    Read `missing_sections` before trusting an empty `open_loops`/`next_actions` — an
    absent heading is not the same as nothing outstanding. If `sequence_truncated` is
    true, the list was cut: read `sequence_path` before acting on it as complete.

    Then do exactly this, in order:
      · Surface `unread_messages` FIRST — they are instructions from other sessions.
        Act or ask, then kb_mark_read. Expired ones: mention in passing, archive, don't act.
      · Write `pin_content` into `.engram-project` at the repo root if it's not there yet.
      · Report in ONE line: where you are · phase · top open loop · what he asked for.
        Do NOT recite the context back — he wrote it. One line, then start work.

    MID-SESSION realign is a DRIFT CHECK, not a reload: compare what you have actually
    been doing against `sequence`, then say plainly what you've been working on, whether
    it is still the priority, and what's next. If you drifted, SAY SO — the drift is the
    information. Don't quietly reconcile it.

    ALSO TELLS YOU WHO ELSE IS WORKING. `working` lists the paths other sessions have
    claimed or been writing, and `needs_the_user` names any room or thread blocked on
    Hiren. Read both before you start: the point of orienting is knowing what NOT to
    pick up, and a collision you could have seen is worse than one you couldn't.

    Returns {resolved, project, resolved_by, phase, open_loops, next_actions, sequence,
    sequence_path, context_path, map_path, last_session, unread_messages, pin_file,
    pin_content, working, needs_the_user, instruction} — or {resolved: false,
    candidates, instruction}.
    """
    result = await (await current_store()).kb_realign(project=project, repo=repo, cwd=cwd, pin=pin)
    if result.get("resolved"):
        _presence_project(str(result.get("project") or ""))
    # Orientation is the moment a session decides what to touch, so it is exactly
    # when it should learn what everyone else is touching. Same data the floor
    # block carries; failure-soft, because coordination must never break the one
    # call a session makes to find its feet.
    try:
        working = list(await _working_now())
        for a in registry.rooms.recent_activity(exclude_session=_speaker_key()):
            if len(working) >= _CLAIMS_IN_FLOOR:
                break
            if a["path"] not in {str(w.get("path")) for w in working}:
                working.append({"path": a["path"], "session": a["session"],
                                "via": "activity"})
        if working:
            result["working"] = working
        me = _require_room_user()
        pending = {
            (r["name"][len(_THREAD_FLOOR_PREFIX):]
             if str(r["name"]).startswith(_THREAD_FLOOR_PREFIX) else r["name"]): r["question"]
            for r in registry.rooms.awaiting_human(me.id)
        }
        if pending:
            result["needs_the_user"] = pending
    except Exception:  # noqa: BLE001
        log.debug("could not attach workspace state to realign", exc_info=True)
    return result


@mcp.tool(meta=_nav_meta)
async def kb_artifacts(project: str | None = None) -> list[dict[str, Any]]:
    """List the saved artifacts in Hiren's knowledge base with their provenance and
    staleness. Call this when the user asks about their artifacts, reports, documents,
    or 'what have I saved' — and when they want to share or revisit one. Artifacts are
    concepts of type 'artifact' under projects/<p>/artifacts/, each recording the exact
    source paths it was built from and the bundle sha it was built against; the server
    computes whether any of those sources have changed since (stale). Optional `project`
    filters to one project id.

    Returns [{path, project, title, description, timestamp, sources, built_from, stale
    (true = a source changed since build, false = current, null = undecidable), shared,
    share_url}], newest first. Open one with kb_read; share it with kb_share_artifact.
    Artifacts are REUSABLE knowledge: kb_read them as sources for new documents, compose
    them into higher-order artifacts, and REBUILD any of them from its stored recipe
    (sources + instruction) against the current brain — git keeps every version.

    When the Navigator widget mounts from this call, say one short line and let the user drive it.
    """
    return await (await current_store()).kb_artifacts(project)


@mcp.tool(meta=_nav_meta)
async def kb_recipes(project: str | None = None) -> list[dict[str, Any]]:
    """List saved recipes — reusable build instructions (ordered sources + an instruction)
    that regenerate a fresh artifact from the CURRENT brain. Call when the user asks about
    their recipes, saved report templates, or 'what can I rebuild'. Reach for kb_recipes,
    NOT kb_artifacts, when they want the reusable BUILD SPEC rather than an already-built
    document: a recipe is the instruction you re-run (run one with the rebuild_artifact
    prompt), an artifact is the output a build produced. Optional `project` filters to one
    project id. Recipes are created by saving one from the Navigator basket or kb_write of a
    type: recipe concept.

    Returns [{path, project, title, description, timestamp, sources, instruction}], newest first."""
    return await (await current_store()).kb_recipes(project)


@mcp.tool()
async def kb_share_artifact(path: str, allow_secrets: bool = False) -> dict[str, Any]:
    """Create a PUBLIC, revocable share link for a saved artifact (type: artifact concept).
    Use when the user says to share, publish, or 'get me a link to' an artifact so someone
    without sign-in can read it (revoke later with kb_unshare_artifact).
    WARNING: this makes THIS document readable by anyone who has the URL — no sign-in, no
    Access gate. Only the artifact's rendered body is exposed; its source paths and the
    rest of the knowledge base stay private. Confirm with the user before sharing anything
    sensitive. Idempotent: re-sharing returns the same link. Revoke anytime with
    kb_unshare_artifact. Before minting the link the body is scanned for likely secrets
    (API keys, tokens, private keys, hardcoded credentials) and sharing is REFUSED if any
    are found — the error names the kinds and line numbers, not the values. Only pass
    allow_secrets=True to override once you have confirmed with the user that the flagged
    content is safe to publish (e.g. an example placeholder), since it bypasses that guard.

    Returns {path, share_url, sha, pushed}.
    """
    result = await (await current_store()).kb_share_artifact(path, allow_secrets)
    # Multi-user: index which tenant owns this public token so the unauthenticated
    # /share route (which has no session) knows which brain to scan.
    if settings.multiuser:
        user = current_user()
        url = result.get("share_url") or ""
        if user is not None and "/share/" in url:
            registry.capabilities.register_public_share(url.rsplit("/", 1)[-1], user.handle)
    return result


@mcp.tool()
async def kb_unshare_artifact(path: str) -> dict[str, Any]:
    """Revoke an artifact's public share link — the URL stops resolving immediately on the
    next push. Use when the user says to unshare, revoke, or make an artifact private again.
    No-op (still succeeds) if it was never shared.

    Returns {path, sha, pushed}.
    """
    return await (await current_store()).kb_unshare_artifact(path)


# ------------------------------------------------------------------ social (M2)
#
# Contacts, DMs, and notifications live in the neutral engram.db (registry.social),
# NOT in anyone's brain. Every tool resolves the caller via current_user() (WHO you
# are, not which brain you own) and refuses outside multiuser. DM bodies are
# secret-scanned at THIS boundary before they touch the shared DB.

_social_bg: set = set()


def _rate_limit_post() -> None:
    """Apply the tighter thread/DM-post cap to a non-owner caller (M2/#13)."""
    if not settings.multiuser:
        return
    token = get_access_token()
    if token is None or not token.subject or token.subject in _OWNER_SUBJECTS:
        return
    limits.check_thread_post(token.subject, settings.thread_post_per_min)


def _require_user():
    """The caller's account, or a KBError teaching that social features need an account."""
    if not settings.multiuser:
        return None  # single-user: social tools are inert (handled per-tool)
    user = current_user()
    if user is None:
        raise KBError(
            "Social features need an Engram account. Accept an invite at "
            f"{settings.public_url}/ and sign in, then try again."
        )
    return user


def _push_notification(user_id: int, kind: str, body: str, ref: str | None = None) -> None:
    """Persist a notification and fan it out (email/telegram) best-effort, off the
    caller's critical path. Fanout never raises; a failed push never fails the tool."""
    social = registry.social
    social.create_notification(user_id, kind, body, ref)
    recipient_user = registry.tenancy  # resolve email for the push
    user = None
    for u in recipient_user.list_users():
        if u.id == user_id:
            user = u
            break
    if user is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    recipient = notify.Recipient(handle=user.handle, email=user.email, telegram_chat_id=None, prefs={})
    task = loop.create_task(
        notify.fanout(settings, recipient, {"kind": kind, "body": body, "ref": ref})
    )
    _social_bg.add(task)
    task.add_done_callback(_social_bg.discard)


@mcp.tool()
async def kb_contacts() -> dict[str, Any]:
    """List your Engram contacts and pending requests. Call when the user asks who
    they're connected to, or before sending a DM to check a contact exists.
    Contacts are mutual-consent: you can only DM someone you're both connected to.

    Returns {contacts: [@handle...], incoming: [@handle...] (requests to accept),
    outgoing: [@handle...] (requests you sent, awaiting)}.
    """
    me = _require_user()
    if me is None:
        return {"contacts": [], "incoming": [], "outgoing": [], "note": "single-user mode"}
    graph = registry.social.list_contacts(me.id)
    handle = registry.tenancy_handle_map()
    return {
        "contacts": [handle.get(i, str(i)) for i in graph["accepted"]],
        "incoming": [handle.get(i, str(i)) for i in graph["incoming"]],
        "outgoing": [handle.get(i, str(i)) for i in graph["outgoing"]],
    }


@mcp.tool()
async def kb_add_contact(handle: str) -> dict[str, Any]:
    """Send a contact request to another Engram user by @handle. They must accept
    before you can DM. If they already requested you, this accepts it (mutual).
    Use when the user says to connect with / add / follow someone.

    Returns {status: 'pending'|'accepted', handle}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Contacts require multi-user mode.")
    other = registry.tenancy.user_by_handle(handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}.")
    contact = registry.social.request_contact(me.id, other.id)
    if contact.status == "accepted":
        _push_notification(other.id, "contact_accepted", f"@{me.handle} is now your contact")
    else:
        _push_notification(other.id, "contact_request", f"@{me.handle} wants to connect")
    return {"status": contact.status, "handle": other.handle}


@mcp.tool()
async def kb_accept_contact(handle: str) -> dict[str, Any]:
    """Accept a pending contact request from @handle. After this you can DM each other.
    Use when the user says to accept / approve a contact request.

    Returns {status: 'accepted', handle}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Contacts require multi-user mode.")
    other = registry.tenancy.user_by_handle(handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}.")
    registry.social.accept_contact(me.id, other.id)
    _push_notification(other.id, "contact_accepted", f"@{me.handle} accepted your request")
    return {"status": "accepted", "handle": other.handle}


@mcp.tool()
async def kb_dm(to: str, message: str) -> dict[str, Any]:
    """Send a direct message to a contact by @handle. You must both be contacts first
    (kb_add_contact). Use when the user says to message / DM / tell another Engram user
    something. The recipient gets a notification (and a push if they've enabled one).

    Returns {sent: true, to, at}.
    """
    me = _require_user()
    if me is None:
        raise KBError("DMs require multi-user mode.")
    other = registry.tenancy.user_by_handle(to.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{to.lstrip('@')}.")
    # Secret-scan at the boundary — the DM lands in the shared DB (and a push email).
    findings = _scan_secrets(message)
    if findings:
        kinds = sorted({k for k, _ in findings})
        raise KBError(
            f"Refusing to send: this message contains what look like secrets ({', '.join(kinds)}). "
            "Remove them — DMs are stored and may be emailed to the recipient."
        )
    _rate_limit_post()
    conv = registry.social.get_or_create_dm(me.id, other.id)
    msg = registry.social.send_message(conv.id, me.id, message)
    preview = message if len(message) <= 80 else message[:77] + "..."
    _push_notification(other.id, "dm", f"@{me.handle}: {preview}", ref=str(conv.id))
    return {"sent": True, "to": other.handle, "at": msg.created}


@mcp.tool()
async def kb_messages(with_handle: str = "", since: str = "") -> dict[str, Any]:
    """Read your DMs. With no argument, lists your conversations (each with the other
    party, last message, and unread count). With with_handle=@someone, returns that
    conversation's messages and marks them read. Call at session start or when the user
    asks about their messages / who messaged them.

    Returns either {conversations: [...]} or {with, messages: [{from, body, at}...]}.
    """
    me = _require_user()
    if me is None:
        return {"conversations": [], "note": "single-user mode"}
    handle = registry.tenancy_handle_map()
    if not with_handle:
        convs = registry.social.list_conversations(me.id)
        out = []
        for c in convs:
            others = [handle.get(m, str(m)) for m in c["members"] if m != me.id]
            last = c["last_message"]
            out.append({
                "with": others[0] if len(others) == 1 else others,
                "title": c["title"],
                "unread": c["unread"],
                "last": (last.body[:80] if last else None),
            })
        return {"conversations": out}
    other = registry.tenancy.user_by_handle(with_handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{with_handle.lstrip('@')}.")
    if not registry.social.are_contacts(me.id, other.id):
        raise KBError(f"You're not contacts with @{other.handle} yet.")
    conv = registry.social.get_or_create_dm(me.id, other.id)
    msgs = registry.social.list_messages(conv.id, me.id, since=since or None)
    registry.social.mark_read(conv.id, me.id)
    return {
        "with": other.handle,
        "messages": [
            {"from": handle.get(m.sender_id, str(m.sender_id)), "body": m.body, "at": m.created}
            for m in msgs
        ],
    }


@mcp.tool()
async def kb_notifications(mark_read: bool = False) -> dict[str, Any]:
    """List your unread Engram notifications (new DMs, contact requests, invite
    acceptances). Pass mark_read=True to mark them all read after showing them.
    Call at session start to surface what happened while you were away.

    Returns {unread: [{kind, body, at}...], counts: {dms, notifications}}.
    """
    me = _require_user()
    if me is None:
        return {"unread": [], "counts": {"dms": 0, "notifications": 0}, "note": "single-user mode"}
    notes = registry.social.list_notifications(me.id, unread_only=True)
    counts = registry.social.unread_counts(me.id)
    if mark_read:
        registry.social.mark_notifications_read(me.id)
    return {
        "unread": [{"kind": n.kind, "body": n.body, "at": n.created, "ref": n.ref} for n in notes],
        "counts": counts,
    }


# ------------------------------------------------------------------ social widget (app-only data plane)
#
# The "Messages" MCP App widget (ui://engram/messages). kb_inbox_card mounts it;
# the app-only tools below are its data plane (visibility:["app"] — invisible to the
# model, zero context). Each resolves the caller via current_user() exactly like the
# conversational social tools, and the widget only ever reaches them over the bridge.

_social_meta = _app_meta  # v3: the Messages card is the unified app's Rooms tab
_social_app_meta = social_app_tool_meta(settings.widget)


def _profile_of(uid: int, umap: dict) -> dict[str, Any]:
    u = umap.get(uid)
    return {
        "handle": u.handle if u else str(uid),
        "display_name": (u.display_name if u else None),
        "avatar_url": (u.avatar_url if u else None),
    }


@mcp.tool(meta=_social_meta)
async def kb_inbox_card() -> dict[str, Any]:
    """Show the user their Engram messages — DMs, contacts, notifications — as a card.
    Call when they ask to see/open their messages, inbox, DMs, or notifications in a
    claude.ai chat. Mounts the unified Engram app on its Rooms tab; after it mounts, say one short line and
    let them use it.

    Returns a compact summary {unread_dms, unread_notifications, contacts}.
    """
    me = _require_user()
    if me is None:
        return {"view": "rooms", "unread_dms": 0, "unread_notifications": 0, "contacts": 0,
                "note": "single-user mode"}
    counts = registry.social.unread_counts(me.id)
    graph = registry.social.list_contacts(me.id)
    return {
        "view": "rooms",  # unified app: this launcher opens the Rooms tab
        "unread_dms": counts.get("dms", 0),
        "unread_notifications": counts.get("notifications", 0),
        "contacts": len(graph["accepted"]),
    }


@mcp.tool(meta=_social_app_meta)
async def social_state() -> dict[str, Any]:
    """App-only data plane for the Messages widget (invisible to the model). The full
    snapshot the widget renders + polls. Never call directly — use kb_inbox_card."""
    me = _require_user()
    if me is None:
        return {"me": None, "contacts": [], "incoming": [], "outgoing": [],
                "conversations": [], "notifications": [], "counts": {"dms": 0, "notifications": 0}}
    umap = {u.id: u for u in registry.tenancy.list_users()}
    graph = registry.social.list_contacts(me.id)
    convs = []
    for c in registry.social.list_conversations(me.id):
        others = [o for o in c["members"] if o != me.id]
        p = _profile_of(others[0], umap) if len(others) == 1 else {"handle": c["title"] or "group"}
        last = c["last_message"]
        # "with" = who the conversation is with (kb_messages' convention; the widget
        # keys its open-DM action on it). Profile fields ride along for rendering.
        convs.append({**p, "with": p.get("handle"), "unread": c["unread"],
                      "last": (last.body[:120] if last else None),
                      "at": (last.created if last else None)})
    return {
        "me": _profile_of(me.id, umap),
        "contacts": [_profile_of(i, umap) for i in graph["accepted"]],
        "incoming": [_profile_of(i, umap) for i in graph["incoming"]],
        "outgoing": [_profile_of(i, umap) for i in graph["outgoing"]],
        "conversations": convs,
        # ref carries the deep-link target (a room name for room_invite/room_closed,
        # a conv id for dm) — the widget's [Open room] action needs it.
        "notifications": [{"kind": n.kind, "body": n.body, "at": n.created, "ref": n.ref}
                          for n in registry.social.list_notifications(me.id, unread_only=True)],
        "counts": registry.social.unread_counts(me.id),
    }


@mcp.tool(meta=_social_app_meta)
async def social_conversation(with_handle: str) -> dict[str, Any]:
    """App-only: one conversation's messages (marks it read). Widget use only."""
    me = _require_user()
    if me is None:
        return {"with": with_handle, "messages": []}
    other = registry.tenancy.user_by_handle(with_handle.lstrip("@"))
    if other is None or not registry.social.are_contacts(me.id, other.id):
        return {"with": with_handle, "messages": []}
    umap = {u.id: u for u in registry.tenancy.list_users()}
    conv = registry.social.get_or_create_dm(me.id, other.id)
    msgs = registry.social.list_messages(conv.id, me.id)
    registry.social.mark_read(conv.id, me.id)
    return {
        "with": other.handle,
        "messages": [{"from": umap[m.sender_id].handle if m.sender_id in umap else str(m.sender_id),
                      "body": m.body, "at": m.created, "mine": m.sender_id == me.id} for m in msgs],
    }


@mcp.tool(meta=_social_app_meta)
async def social_send(to: str, message: str) -> dict[str, Any]:
    """App-only: send a DM from the widget. Same contact + secret-scan guards as kb_dm."""
    return await kb_dm(to, message)


@mcp.tool(meta=_social_app_meta)
async def social_accept(handle: str) -> dict[str, Any]:
    """App-only: accept a contact request from the widget."""
    return await kb_accept_contact(handle)


@mcp.tool(meta=_social_app_meta)
async def social_mark_read() -> dict[str, Any]:
    """App-only: mark all notifications read from the widget."""
    me = _require_user()
    if me is not None:
        registry.social.mark_notifications_read(me.id)
    return {"ok": True}


@mcp.tool()
async def kb_move_project(project: str, folder: str = "") -> dict[str, Any]:
    """Put a project in a folder — real directories, one project in exactly one place.

    Call when the user wants to file, group or organize projects ("put the client work in
    an alt-inc folder", "move pixelpuri to personal"). `folder` is a folder name like
    'personal' or 'alt-inc'; pass "" to move it back to the top level. Folders are actual
    directories on disk, so the brain stays browsable in git and any file manager.

    The project's ID doesn't change, so nothing that refers to it breaks — kb_load, the
    `.engram-project` pin and the office all keep working. Links across the bundle are
    re-expressed automatically so they stay correct at the new depth.

    Returns {project, folder, from, to, links_rewritten, sha, pushed}.
    """
    return await (await current_store()).kb_move_project(project, folder)


@mcp.tool()
async def kb_project_status(project: str, status: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    """Set a project's `status` (and optional free-form `tags`).

    `status`: 'active' | 'paused' | 'archived' — archiving tucks a finished project out of
    the way in listings without deleting anything. Use kb_move_project to file it in a
    folder; tags here are just optional labels, not the organizing structure.

    Returns {project, tags, status, sha, pushed}.
    """
    return await (await current_store()).kb_tag_project(project, tags, status)


@mcp.tool()
async def kb_attach_project(project: str = "", description: str = "") -> dict[str, Any]:
    """Anchor THIS session to a project — call at session start when no project is pinned.

    Every session should be working on a known project so its work is logged somewhere
    coherent and it shows up correctly in the workspace. If the repo has no
    `.engram-project` file, ask the user which project this is (kb_projects lists them),
    then call this with that id — or with a NEW id plus a one-line `description` to start
    a project from scratch. Call with no arguments to get the list to choose from.

    After it returns, WRITE the returned `pin_content` to a file named `.engram-project`
    at the repo root (Claude Code: a one-line file) — that pins this repo to the project
    for this and every future session, so nobody has to be asked again.

    Returns {project, created, pin_file, pin_content, instruction} or {projects: [...]}.
    """
    store = await current_store()
    if not project:
        return {
            "projects": await store.kb_projects(),
            "instruction": "Ask the user which of these this session is working on (or a "
                           "new name + description), then call kb_attach_project again.",
        }
    result = await store.ensure_project(project, description)
    _presence_project(project)
    return {
        **result,
        "pin_file": ".engram-project",
        "pin_content": result["project"],
        "instruction": f"Write '{result['project']}' into a file named .engram-project at "
                       "the repo root so this repo stays attached in future sessions.",
    }


# ------------------------------------------------------------------ public work & discovery (M5)
#
# Three tiers of reach: private (default), contacts, public. "Public" here means any
# SIGNED-IN Engram user — never the open web. Publishing is one-way, so it is always an
# explicit per-item act (kb_publish), secret-scanned, and auditable (kb_public).
# Questions from other people are QUARANTINED in the neutral DB (registry.discovery) —
# a stranger's text never auto-commits into anyone's git brain.


async def _profile_of_handle(handle: str, viewer_id: int | None) -> dict[str, Any]:
    """Public profile card for a user: identity + follow stats (no brain contents)."""
    user = registry.tenancy.user_by_handle(handle.lstrip("@"))
    if user is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}.")
    counts = registry.discovery.follow_counts(user.id)
    return {
        "handle": user.handle,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "bio": user.bio,
        "followers": counts.get("followers", 0),
        "following": counts.get("following", 0),
        "is_following": bool(viewer_id and registry.discovery.is_following(viewer_id, user.id)),
    }


async def _public_work_of(handle: str) -> list[dict[str, Any]]:
    """Everything a user has marked public, from THEIR store (never their private work)."""
    store = await registry.store_for_handle(handle.lstrip("@"))
    listing = await store.kb_public()
    return listing.get("public", [])


@mcp.tool()
async def kb_publish(path: str, visibility: str = "public") -> dict[str, Any]:
    """Publish (or unpublish) part of YOUR brain. Use when the user says to make something
    public/private, share their work, or put a project on their profile.

    `visibility`: 'public' (any signed-in Engram user can read + discover it), 'contacts'
    (only accepted contacts), or 'private' (the default — unpublishes). Pass a project's
    context.md to set the DEFAULT for that whole project; individual concepts override it.
    Pass a FOLDER path ('projects/personal') to set the default for every project inside —
    folders are audiences: e.g. kb_publish('projects/personal', 'private') keeps personal
    work private while the team folder stays shared. Specific always beats general:
    concept > project > folder > server default.
    Publishing is one-way in practice — anyone who could read it may have copied it — so
    confirm with the user before publishing anything sensitive. The body is secret-scanned
    and publishing is REFUSED if it looks like it contains credentials. Session mail,
    threads, workspace and inbox can never be published, even inside a public project.

    Returns {path, visibility, applies_to, sha, pushed}.
    """
    return await (await current_store()).kb_publish(path, visibility)


@mcp.tool()
async def kb_public() -> dict[str, Any]:
    """Audit what you've made visible to other people — call when the user asks "what's
    public?", before publishing more, or to check nothing leaked.

    Returns {public: [...], contacts: [...]} of {path, title, description, type, project}.
    """
    return await (await current_store()).kb_public()


@mcp.tool()
async def kb_explore(handle: str = "", query: str = "") -> dict[str, Any]:
    """Discover people on Engram and their public work. Call when the user wants to find
    someone, browse what others have published, or look at a person's profile.

    No arguments: lists people with public work (the directory). With `handle`: that
    person's profile + everything they've published. With `query`: SEMANTIC search across
    everyone's public work — use this reflexively before solving a hard problem ("has
    anyone on the team hit this?"); results carry `engine:"semantic"` and a score, or
    fall back to substring matching when the vector backend is down. Read a specific
    item with kb_read_public.

    Returns {people: [...]} or {profile, public_work: [...]} or {results: [...], engine}.
    """
    me = current_user()
    viewer_id = me.id if me else None
    if handle and not query:
        return {"profile": await _profile_of_handle(handle, viewer_id), "public_work": await _public_work_of(handle)}
    if query:
        # The save (v3 Wave 5): semantic first — meaning-level matches across the
        # team's public work, own points excluded. Substring scan is the fallback.
        my_store = await current_store()
        me_tenant = my_store.settings.tenant_id
        if not handle and my_store.semantic is not None:
            sem = await to_thread.run_sync(
                lambda: my_store.semantic.search_team(query, exclude_user=me_tenant)
            )
            if sem is not None:
                return {"results": sem, "engine": "semantic"}
        handles = [handle.lstrip("@")] if handle else [u.handle for u in registry.tenancy.list_users()]
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        for h in handles:
            if not handle and h == me_tenant:
                continue  # your own work is kb_search's job, not discovery's
            try:
                for item in await _public_work_of(h):
                    hay = f"{item.get('title','')} {item.get('description','')} {item.get('path','')}".lower()
                    if needle in hay:
                        hits.append({**item, "handle": h})
            except KBError:
                continue
        return {"results": hits[:50], "engine": "text"}
    people = []
    for u in registry.tenancy.list_users():
        if u.status != "active":
            continue
        if me and u.id == me.id:
            continue  # discovery shows OTHER people — you are not a search result to yourself
        try:
            work = await _public_work_of(u.handle)
        except KBError:
            work = []
        if not work:
            continue  # only surface people who've actually published something
        counts = registry.discovery.follow_counts(u.id)
        people.append({
            "handle": u.handle, "display_name": u.display_name, "avatar_url": u.avatar_url,
            "bio": u.bio, "followers": counts.get("followers", 0),
            "public_projects": len({i.get("project") for i in work if i.get("project")}),
            "public_items": len(work),
            "is_following": bool(viewer_id and registry.discovery.is_following(viewer_id, u.id)),
        })
    return {"people": people}


@mcp.tool()
async def kb_read_public(handle: str, path: str) -> dict[str, Any]:
    """Read ONE concept from another user's PUBLIC work — no permission needed, that's what
    public means. Use when the user wants to see/discuss something they found via kb_explore
    ("open @amiya's search decision", "what does @bob say about X?"). You can then reason
    about it, summarize it, or answer the user's question from it.

    Refuses anything not actually published. To ask the AUTHOR a question about it, use
    kb_ask. For private work someone shared with you specifically, use kb_guest_read.

    Returns {handle, path, title, type, project, content}.
    """
    owner = registry.tenancy.user_by_handle(handle.lstrip("@"))
    if owner is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}.")
    store = await registry.store_for_handle(owner.handle)
    if await store.effective_visibility(path) != "public":
        raise KBError(
            f"'{path}' is not public. Only work @{owner.handle} has published can be read this "
            "way — ask them for access with kb_request_context, or ask a question with kb_ask."
        )
    concept = await store.kb_read(path, depth=0)  # depth 0: never expand into unpublished neighbours
    meta = concept.get("meta") or {}
    # Team-test metric #2 ("did anyone READ someone else's work?") — one row per
    # cross-user read, surfaced on /dashboard/ops. Best-effort, never blocks a read.
    try:
        me = current_user()
        if me is not None:
            registry.discovery.log_public_read(me.id, owner.id, concept["path"])
    except Exception:  # noqa: BLE001
        log.debug("public-read metric failed", exc_info=True)
    return {
        "handle": owner.handle, "path": concept["path"],
        "title": str(meta.get("title") or ""), "type": str(meta.get("type") or ""),
        "project": str(meta.get("project") or ""), "content": concept.get("content") or "",
    }


@mcp.tool()
async def kb_common_ground(handle: str) -> dict[str, Any]:
    """What do I and this person have in COMMON? Explainable work-overlap: pairs of
    (your concept, their public concept) that are semantically close — never an
    opaque similarity score. Call when the user asks what they share with someone,
    before opening a room with them, or to explain WHY a person surfaced in discovery.

    Every pair is a checkable claim: read theirs with kb_read_public, yours with
    kb_read. Empty overlap is a real answer (no manufactured matches).

    Returns {handle, pairs: [{mine:{path,title}, theirs:{path,title,description}, score}]}.
    """
    me = _require_user()
    other = handle.lstrip("@").lower()
    target = registry.tenancy.user_by_handle(other)
    if target is None:
        raise KBError(f"No Engram account with the handle '@{other}'. kb_explore() lists people.")
    if me is not None and target.id == me.id:
        raise KBError("Common ground is with SOMEONE ELSE — kb_search covers your own brain.")
    my_store = await current_store()
    if my_store.semantic is None:
        return {"handle": other, "pairs": [], "note": "semantic backend offline — no overlap scan"}
    pairs = await to_thread.run_sync(lambda: my_store.semantic.common_ground(other))
    return {"handle": other, "pairs": pairs or []}


@mcp.tool()
async def kb_follow(handle: str, unfollow: bool = False) -> dict[str, Any]:
    """Follow (or unfollow) another Engram user, so their newly published work shows up in
    your kb_feed. One-directional — no approval needed, unlike contacts (which gate DMs).

    Returns {handle, following: bool}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Following requires multi-user mode.")
    other = registry.tenancy.user_by_handle(handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}.")
    if unfollow:
        registry.discovery.unfollow(me.id, other.id)
        return {"handle": other.handle, "following": False}
    registry.discovery.follow(me.id, other.id)
    _push_notification(other.id, "new_follower", f"@{me.handle} followed you")
    return {"handle": other.handle, "following": True}


@mcp.tool()
async def kb_feed(limit: int = 20) -> dict[str, Any]:
    """What the people you follow have published recently. Call at session start or when
    the user asks what's new / what others are working on.

    Returns {items: [{handle, title, description, path, project, updated}]}.
    """
    me = _require_user()
    if me is None:
        return {"items": [], "note": "single-user mode"}
    umap = {u.id: u for u in registry.tenancy.list_users()}
    items: list[dict[str, Any]] = []
    for uid in registry.discovery.following(me.id):
        u = umap.get(uid)
        if u is None:
            continue
        try:
            for item in await _public_work_of(u.handle):
                items.append({**item, "handle": u.handle, "display_name": u.display_name,
                              "avatar_url": u.avatar_url})
        except KBError:
            continue
    items.sort(key=lambda i: str(i.get("updated") or ""), reverse=True)
    return {"items": items[: max(1, min(limit, 100))]}


@mcp.tool()
async def kb_ask(handle: str, path: str, question: str) -> dict[str, Any]:
    """Ask the author a question about a specific piece of their public work. Use when the
    user has read something (kb_read_public) and wants to ask the person about it — the
    question lands in THEIR inbox to answer, and you'll see the answer in kb_asks.

    They don't need to be a contact — asking about public work is open. Their brain is not
    modified: the question is held separately until they choose to act on it.

    Returns {ask_id, to, path}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Asking requires multi-user mode.")
    owner = registry.tenancy.user_by_handle(handle.lstrip("@"))
    if owner is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}.")
    store = await registry.store_for_handle(owner.handle)
    if await store.effective_visibility(path) != "public":
        raise KBError(f"'{path}' is not public — you can only ask about published work.")
    findings = _scan_secrets(question)
    if findings:
        raise KBError("Refusing to send: your question looks like it contains a credential.")
    ask = registry.discovery.create_ask(me.id, owner.id, path, question)
    preview = question if len(question) <= 80 else question[:77] + "..."
    _push_notification(owner.id, "question", f"@{me.handle} asked about {path}: {preview}", ref=str(ask.id))
    return {"ask_id": ask.id, "to": owner.handle, "path": path}


@mcp.tool()
async def kb_answer(ask_id: int, answer: str) -> dict[str, Any]:
    """Answer a question someone asked about your public work (see kb_asks). The asker is
    notified. Only you can answer questions addressed to you.

    Returns {ask_id, to, status}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Answering requires multi-user mode.")
    findings = _scan_secrets(answer)
    if findings:
        raise KBError("Refusing to send: your answer looks like it contains a credential.")
    ask = registry.discovery.answer_ask(int(ask_id), me.id, answer)
    umap = {u.id: u for u in registry.tenancy.list_users()}
    asker = umap.get(ask.asker_id)
    if asker is not None:
        preview = answer if len(answer) <= 80 else answer[:77] + "..."
        _push_notification(asker.id, "answer", f"@{me.handle} answered your question: {preview}",
                           ref=str(ask.id))
    return {"ask_id": ask.id, "to": (asker.handle if asker else str(ask.asker_id)), "status": ask.status}


@mcp.tool()
async def kb_asks() -> dict[str, Any]:
    """Questions about your public work that await an answer, plus questions you asked
    others (with their answers). Call when the user asks about questions/answers, or at
    session start if kb_load flagged open ones.

    Returns {to_answer: [...], i_asked: [...]} of {id, from/to, path, question, answer, status, created}.
    """
    me = _require_user()
    if me is None:
        return {"to_answer": [], "i_asked": [], "note": "single-user mode"}
    umap = {u.id: u for u in registry.tenancy.list_users()}

    def _h(uid: int) -> str:
        u = umap.get(uid)
        return u.handle if u else str(uid)

    return {
        "to_answer": [
            {"id": a.id, "from": _h(a.asker_id), "path": a.path, "question": a.question,
             "status": a.status, "created": a.created}
            for a in registry.discovery.list_asks_for(me.id, open_only=True)
        ],
        "i_asked": [
            {"id": a.id, "to": _h(a.owner_id), "path": a.path, "question": a.question,
             "answer": a.answer, "status": a.status, "created": a.created}
            for a in registry.discovery.list_asks_by(me.id)
        ],
    }


# ---------------------------------------------------- rooms (v3 Wave 4: live joins across brains)
# A room is where two+ people's Claudes converge live AND can reach back into the
# async substrate mid-conversation (kb_room_search/kb_room_fetch over room-scoped
# grants). Cross-user state, so rooms live in the neutral engram.db — never in a
# git brain. Long-poll is the only waiting primitive; never client-side polling.

from engram_server.teamwork import room_notify as _room_notify  # noqa: E402
from engram_server.teamwork import room_wait as _room_wait  # noqa: E402
from engram_server.teamwork import room_wait_any as _wait_any_room  # noqa: E402


def _require_room_user():
    me = _require_user()
    if me is None:
        raise KBError(
            "Rooms are a multi-user feature (live rooms between Engram accounts). "
            "Same-brain session rendezvous still works via kb_thread_post/kb_thread_read."
        )
    return me


def _room_of(name: str):
    room = registry.rooms.room_by_name(name.strip().lower())
    if room is None:
        raise KBError(f"No room named '{name}'. kb_rooms() lists yours; kb_room_open starts one.")
    return room


def _require_member(room, me) -> None:
    """Explicit membership gate. Some tool bodies are ALSO protected incidentally
    (their audit post_turn enforces membership) — but that protection is a side
    effect a refactor could silently remove (sec-review), so guest-content tools
    check membership FIRST, before any granted data is assembled."""
    if not any(m["user_id"] == me.id for m in registry.rooms.members(room.id)):
        raise KBError(
            f"You are not a member of room '{room.name}' — ask a member for a "
            "kb_room_invite before touching its granted content."
        )


def _room_scan(text: str, what: str) -> None:
    findings = _scan_secrets(text)
    if findings:
        kinds = sorted({k for k, _ in findings})
        raise KBError(
            f"Refusing: this {what} contains what look like secrets ({', '.join(kinds)}). "
            "Room content is visible to every member and lands in the shared DB."
        )


_SPEAKER_ATTR = "_engram_speaker_key"

# ONE PROTOCOL, TWO SURFACES. Threads and rooms are the same conversation with
# different durability: a thread transcript lives in GIT (permanent, versioned,
# the record behind the Office conference rooms and the meetings widget), while a
# room lives in the neutral DB (cross-user, coordination-shaped). Everything ELSE
# that made rooms work today — whose turn it is, who is listening, who has gone,
# escalating to the person — is protocol, not storage, and threads deserve all of
# it.
#
# So the coordination state for a thread lives in the room tables under a hidden
# shadow room. No extra git writes (presence would otherwise commit on every
# read, and the brain has exactly one writer), no duplicated protocol, and the
# transcript stays exactly where it was.
_THREAD_FLOOR_PREFIX = "thread--"

# WHO IS WORKING WHERE, delivered where sessions already look.
#
# kb_claim has existed since the workspace wave: advisory, 30-minute TTL, already
# wired into kb_write/kb_edit to warn on a foreign claim. What was missing is that
# seeing it required CALLING kb_claims — and no session ever did, because nothing
# prompted it. An advisory signal nobody fetches is not a signal.
#
# So it rides in `floor`, the block every room and thread result already returns
# and every session already reads. No new storage, no new call, no discipline.
# Capped and cached because this lands in an LLM's context on every single turn:
# the cost of the signal has to stay far below the cost of the collision.
_CLAIMS_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}
_CLAIMS_TTL_S = 10.0
_CLAIMS_IN_FLOOR = 8


_ACTIVITY_THROTTLE_S = 60.0
_activity_last: dict[tuple[str, str], float] = {}


def _session_label(key: str) -> str:
    """The friendly name a session gave itself in any room or thread, so derived
    activity reads as 'mac-a' rather than an opaque key."""
    if not key:
        return ""
    try:
        return registry.rooms.speaker_label(key) or ""
    except Exception:  # noqa: BLE001
        return ""


def _note_activity(path: str) -> None:
    """Record that this session just wrote to `path`.

    Derived, not declared: working IS the announcement. Throttled per
    (session, path) because a burst of edits to one file is one fact, not twenty.
    Failure-soft — coordination must never be able to break a write."""
    key = _speaker_key()
    if not key or not path:
        return
    try:
        now = time.monotonic()
        slot = (key, path)
        if now - _activity_last.get(slot, 0.0) < _ACTIVITY_THROTTLE_S:
            return
        _activity_last[slot] = now
        me = current_user()
        if me is None:
            return
        registry.rooms.record_activity(me.id, key, path, label=_session_label(key))
    except Exception:  # noqa: BLE001 — never break a write over bookkeeping
        log.debug("activity note failed for %s", path, exc_info=True)


def _clobber_warning(path: str, base_hash: str = "") -> str | None:
    """Did another live session write here recently, and did we write blind?

    `base_hash` is optimistic concurrency and it WORKS — but it is opt-in, so
    omitting it means last-write-wins silently, which is the one outcome nobody
    ever wants. LangGraph refuses a concurrent write to a field with no declared
    merge policy rather than guessing a winner; we cannot refuse (nothing here
    may block a write), so we do the next most useful thing and say it out loud.

    Only fires when there is a REAL other writer, so it stays rare enough to be
    worth reading. A warning that appears on every write is wallpaper."""
    if base_hash:
        return None  # they asked for the guard; it either held or already refused
    try:
        mine = _speaker_key()
        recent = [
            a for a in registry.rooms.recent_activity(exclude_session=mine)
            if a["path"] == path
        ]
    except Exception:  # noqa: BLE001
        return None
    if not recent:
        return None
    who = ", ".join(sorted({str(a["session"]) for a in recent}))
    return (
        f"{who} also wrote to this path in the last "
        f"{registry.rooms.ACTIVITY_TTL_MIN} minutes, and this write passed no "
        "`base_hash`, so it overwrote whatever was there without checking. Their "
        "change may be gone. Re-read the file to see what it says now, and pass "
        "kb_read's `hash` as `base_hash` on the next write so a conflict is "
        "refused instead of silently applied."
    )


async def _working_now() -> list[dict[str, Any]]:
    """Live advisory claims, compact, for the conversation state block. Never
    raises — coordination must not be able to break the conversation it serves."""
    now = time.monotonic()
    if now - float(_CLAIMS_CACHE["at"]) < _CLAIMS_TTL_S:
        return list(_CLAIMS_CACHE["rows"])
    try:
        rows = await (await current_store()).kb_claims()
    except Exception:  # noqa: BLE001
        log.debug("claims unavailable for floor", exc_info=True)
        return list(_CLAIMS_CACHE["rows"])
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("stale"):
            continue  # a claim past its TTL is not news, it's litter
        out.append({
            "path": r.get("path"),
            "session": r.get("session"),
            "note": str(r.get("note") or "")[:80],
            "minutes_ago": int(r.get("age_min") or 0),
            "via": "claim",  # stated intent, before the work
        })
        if len(out) >= _CLAIMS_IN_FLOOR:
            break
    _CLAIMS_CACHE["at"] = now
    _CLAIMS_CACHE["rows"] = out
    return out


_HANDOFF_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}
_HANDOFF_TTL_S = 30.0
_HANDOFF_FRESH_MIN = 120


async def _recent_handoffs() -> list[dict[str, Any]]:
    """Handoffs from the last couple of hours, compact. Never raises."""
    now = time.monotonic()
    if now - float(_HANDOFF_CACHE["at"]) < _HANDOFF_TTL_S:
        return list(_HANDOFF_CACHE["rows"])
    try:
        rows = await (await current_store()).kb_recent_handoffs(limit=5)
    except Exception:  # noqa: BLE001
        log.debug("handoffs unavailable for floor", exc_info=True)
        return list(_HANDOFF_CACHE["rows"])
    out: list[dict[str, Any]] = []
    for h in rows:
        created = str(h.get("created") or "")
        if created and not _is_fresh_iso(created, _HANDOFF_FRESH_MIN):
            continue
        out.append({
            "from": h.get("from"),
            "summary": str(h.get("summary") or "")[:160],
            "path": h.get("path"),
            "created": created,
        })
        if len(out) >= 2:
            break
    _HANDOFF_CACHE["at"] = now
    _HANDOFF_CACHE["rows"] = out
    return out


def _is_fresh_iso(stamp: str, minutes: int) -> bool:
    try:
        seen = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - seen) <= timedelta(minutes=minutes)


async def _with_working(floor: dict[str, Any]) -> dict[str, Any]:
    """Attach who is working where: declared claims AND derived activity.

    Both, because they answer different questions. A claim is INTENT stated
    before the work, which is the only thing that can prevent a collision rather
    than report one — but it depends on someone remembering to make it. Activity
    is EVIDENCE from the work itself, which needs no discipline but can only be
    backward-looking. Marked `via` so a reader can tell a promise from a fact.

    Claims win on conflict: if someone both claimed a path and wrote to it, the
    claim carries their note, which says more than the write does.

    Absent entirely when nothing is happening, so a quiet workspace costs nothing.
    """
    rows = list(await _working_now())
    claimed = {str(r.get("path")) for r in rows}
    try:
        for a in registry.rooms.recent_activity(exclude_session=_speaker_key()):
            if len(rows) >= _CLAIMS_IN_FLOOR:
                break
            if a["path"] in claimed:
                continue
            rows.append({
                "path": a["path"],
                "session": a["session"],
                "via": "activity",  # observed writes, after the fact
            })
    except Exception:  # noqa: BLE001 — never break a turn over bookkeeping
        log.debug("activity unavailable for floor", exc_info=True)
    if rows:
        floor["working"] = rows

    # HANDOFFS, but only when they change what you would do. Earlier today a
    # session was told "the holder has gone — take the floor" and had no idea
    # what that holder had been doing. A handoff is exactly that missing context.
    # Attached when the conversation is stalled (someone left, so their notes are
    # the point) — not on every turn, where it would be wallpaper.
    if floor.get("stalled"):
        notes = await _recent_handoffs()
        if notes:
            floor["handoffs"] = notes
            floor["do_next"] = (
                str(floor.get("do_next") or "")
                + f" A recent handoff from {notes[0]['from']} may say what they "
                "were doing — kb_read its path before you pick the work up."
            )
    return floor

# Mirrors the room turn budget / hard cap, for the same reason: agent
# conversations terminate because something makes them terminate.
_THREAD_TURN_BUDGET = 40
_THREAD_TURN_CAP = 200


def _thread_floor_id(thread: str) -> int | None:
    """The shadow room carrying a thread's floor state, created on first use.
    Returns None if anything goes wrong — coordination must never break the
    conversation it is meant to help."""
    name = f"{_THREAD_FLOOR_PREFIX}{thread.strip().lower()}"[:64]
    try:
        me = _require_room_user()
        existing = registry.rooms.room_by_name(name)
        if existing is not None:
            return existing.id
        return registry.rooms.open_room(
            me.id, name, goal=f"floor state for thread {thread}",
        ).id
    except Exception:  # noqa: BLE001 — never break a thread over its floor
        log.debug("thread floor unavailable for %s", thread, exc_info=True)
        return None

# The host hard-kills any tools/call at ~60s (measured on claude.ai during the
# MCP-Apps work — see the PARK pattern playbook in the brain). A long-poll that
# outlives the call does not "wait longer", it DIES, and the caller gets a
# transport error after its turn has already posted. So the ceiling sits safely
# inside that, and a timeout is answered with "call again from this cursor"
# rather than a longer wait. The dashboard's browser long-poll is not subject to
# this and keeps the bus-level clamp.
_MAX_MCP_WAIT_S = 45


def _wait_window(seconds: int) -> int:
    """Clamp a caller's requested wait into a window that survives the host."""
    return max(1, min(_MAX_MCP_WAIT_S, seconds))


# A listen is advertised for the poll's length PLUS this, so a session looping its
# long-poll stays continuously "listening" across the 1-3s gap between calls
# instead of flickering out and back. An early return clears the window
# explicitly — the grace covers re-calls, never a session that has gone to act.
_LISTEN_GRACE_S = 10


def _speaker_key() -> str:
    """A stable identifier for THIS MCP session, for floor control.

    Two Claude sessions belonging to one user are the same `user_id` and the same
    handle, so nothing in the room could previously tell them apart — which is why
    wait_for_reply could never see the other one. The MCP session object lives for
    the length of the connection, so we stamp a key onto it once and reuse it. Not
    `id()`: CPython recycles addresses, and a recycled key would silently merge two
    different sessions. Failure-soft — if there is no request context (tests, the
    scheduler), returns '' and every caller degrades to the old open-floor
    behaviour rather than erroring."""
    try:
        session = mcp.get_context().session
    except Exception:  # noqa: BLE001 — no active request context
        return ""
    if session is None:
        return ""
    try:
        key = getattr(session, _SPEAKER_ATTR, "")
        if not key:
            key = uuid.uuid4().hex[:8]
            setattr(session, _SPEAKER_ATTR, key)
        return str(key)
    except Exception:  # noqa: BLE001 — never break a tool call over identity
        return ""


def _room_view(room, user_id: int | None = None) -> dict[str, Any]:
    handles = registry.tenancy_handle_map()
    members = registry.rooms.members(room.id)
    view = {
        "name": room.name, "goal": room.goal, "exit_condition": room.exit_condition,
        "status": room.status, "turn_budget": room.turn_budget, "hard_cap": room.hard_cap,
        "creator": handles.get(room.creator_id, "?"),
        "members": [handles.get(m["user_id"], "?") for m in members],
        "created": room.created, "closed_at": room.closed_at, "outcome": room.outcome,
        "grants": [
            {"by": handles.get(g["grantor_id"], "?"), "path": g["path_prefix"]}
            for g in registry.rooms.grants_for(room.id)
        ],
    }
    return view


def _turn_view(t, handles: dict[int, str]) -> dict[str, Any]:  # noqa: D401
    sess = t.session or ""
    is_claude = sess == "claude" or sess.startswith("claude:")
    is_relay = sess.startswith("relay:")
    via = ("human" if (sess in ("web", "app") or sess.startswith("dashboard:")
                       or is_relay)
           else ("claude" if is_claude else ""))
    out = {"id": t.id, "author": handles.get(t.user_id, "?"), "kind": t.kind,
           "body": t.body, "created": t.created}
    if via:
        out["via"] = via  # who actually wrote it: the person, or their Claude
    if is_relay:
        # Their words, but they were not in the room — say so rather than let the
        # transcript imply they were sitting in it.
        out["relayed"] = True
    if sess.startswith("claude:"):
        # WHICH Claude. Two sessions of one user post under the same handle, so
        # without this a transcript of a two-session room reads as one voice
        # talking to itself — which is exactly how it looked before floor control.
        out["speaker"] = sess.split(":", 1)[1]
    refs = list(getattr(t, "refs", ()) or ())
    if refs:
        # Attached concepts — read them with kb_read (your own) or kb_room_fetch
        # (a member's, if granted). Sharing a path beats pasting a document.
        out["refs"] = refs
    return out


@mcp.tool()
async def kb_room_open(
    name: str,
    goal: str,
    exit_condition: str = "",
    invite: str = "",
    grant: str = "",
    turn_budget: int = 40,
    hard_cap: int = 200,
) -> dict[str, Any]:
    """Open a live ROOM — a shared space where your Claude and your teammates' Claudes
    talk in real time AND can search each other's granted work mid-conversation. Use
    when the user wants to work something out with specific people ("open a room with
    riya about the deploy"), or when a discovery (kb_explore / kb_common_ground) is
    worth a live conversation.

    `goal` is REQUIRED and `exit_condition` strongly encouraged — rooms have a turn
    budget precisely so agent conversations terminate instead of politely agreeing
    forever. `invite`: comma-separated @handles — each gets a notification (Chrome
    extension + email) with the room name. `grant`: comma-separated path prefixes of
    YOUR brain (e.g. 'projects/slate') that other members' Claudes may search/read
    WHILE THIS ROOM IS OPEN — auto-revoked on close, every access logged as a
    visible turn. Never grant a whole brain; messages/inbox/workspace can never be
    granted.

    Then post with kb_room_post (wait_for_reply=True), and close with kb_room_close —
    which offers the outcome back to the user for their brain.

    Returns {room, invited: [...]}.
    """
    me = _require_room_user()
    _room_scan(f"{goal}\n{exit_condition}", "room goal")
    room = registry.rooms.open_room(
        me.id, name, goal, exit_condition=exit_condition,
        turn_budget=turn_budget, hard_cap=hard_cap,
    )
    invited: list[str] = []
    for h in [x.strip().lstrip("@").lower() for x in invite.split(",") if x.strip()]:
        other = registry.tenancy.user_by_handle(h)
        if other is None or other.id == me.id:
            continue
        if registry.rooms.invite(room.id, me.id, other.id):
            invited.append(h)
            _push_notification(
                other.id, "room_invite",
                f"@{me.handle} invited you to room '{room.name}': {room.goal[:120]}",
                ref=room.name,
            )
    for p in [x.strip() for x in grant.split(",") if x.strip()]:
        registry.rooms.add_grant(room.id, me.id, p)
    return {"room": _room_view(room), "invited": invited}


@mcp.tool()
async def kb_rooms(include_closed: bool = False, wait_seconds: int = 0) -> dict[str, Any]:
    """List the rooms you are in — live first — with unread counts and budget state.
    Call when the user asks what's happening, whether anyone needs them, or to find a
    room by name. `your_turn: true` means that room is WAITING ON YOU — somebody
    handed you the floor and is not going to speak again until you do; deal with those
    first.

    MONITORING SEVERAL ROOMS AT ONCE: pass wait_seconds (~25-45) and this returns the
    moment ANY of your rooms sees a turn, instead of you picking one room to block on
    and going deaf to the rest. That is the right way to watch more than one
    conversation — never poll in a loop. Capped at 45s (the host kills longer calls);
    to keep waiting, call again rather than asking for a bigger number.

    Returns {rooms: [{name, goal, status, members, unread, messages_used, turn_budget,
    last_turn, your_turn}], waiting_on_you, woke_on}."""
    me = _require_room_user()
    handles = registry.tenancy_handle_map()
    key = _speaker_key()
    def _visible(rows: list[dict]) -> list[dict]:
        # Shadow rooms hold thread floor state and are plumbing, not conversations.
        # Listing them would put a duplicate entry beside every thread.
        return [r for r in rows if not str(r["name"]).startswith(_THREAD_FLOOR_PREFIX)]

    woke_on = ""
    if wait_seconds > 0:
        live = _visible(registry.rooms.list_rooms_for(me.id, include_closed=False))
        # Only park if there is genuinely nothing new. Waiting on top of unread
        # turns is how a session misses the message it was called to read.
        if live and not any(row["unread"] for row in live):
            names = {row["id"]: row["name"] for row in live}
            woke = await _wait_any_room(list(names), _wait_window(wait_seconds))
            woke_on = names.get(woke, "") if woke else ""
    rooms = []
    for row in _visible(registry.rooms.list_rooms_for(me.id, include_closed=include_closed)):
        entry = {
            "name": row["name"], "goal": row["goal"], "status": row["status"],
            "members": [handles.get(uid, "?") for uid in row["member_ids"]],
            "unread": row["unread"], "messages_used": row.get("messages_used", 0),
            "turn_budget": row["turn_budget"], "hard_cap": row["hard_cap"],
            "last_turn": row.get("last_turn"),
        }
        if row["status"] == "open":
            state = registry.rooms.floor_state(row["id"], key)
            if key:
                entry["your_turn"] = state["is_you"]
            if state["awaiting_human"]:
                entry["needs_the_user"] = state["awaiting_human"]
        rooms.append(entry)
    out = {
        "rooms": rooms,
        "waiting_on_you": [r["name"] for r in rooms if r.get("your_turn")],
    }
    if woke_on:
        out["woke_on"] = woke_on
    # Rooms frozen on the PERSON. Put these to the user in chat and relay their
    # answer with kb_room_relay_answer: a blocked room is otherwise invisible to
    # them unless they happen to open a web page, and they mostly don't.
    needs = {r["name"]: r["needs_the_user"] for r in rooms if r.get("needs_the_user")}
    if needs:
        out["needs_the_user"] = needs
    return out


@mcp.tool()
async def kb_room_post(
    room: str,
    message: str,
    refs: list[str] | None = None,
    wait_for_reply: bool = False,
    wait_seconds: int = 25,
    speaker: str = "",
    hand_to: str = "",
    ask_human: str = "",
    expect_cursor: int = 0,
) -> dict[str, Any]:
    """Post a turn into a room. PREFER wait_for_reply=True (wait_seconds ~25-45): it
    long-polls SERVER-SIDE for someone else's next turn — free while idle, and it
    returns the INSTANT they speak, not after the timeout. Never poll in a tight loop.

    Waits are capped at 45s because the host kills any tool call around 60s: asking for
    longer doesn't wait longer, it dies. If you time out and still want to wait, call
    again — writing nothing in between — rather than asking for a bigger number.

    TAKING TURNS. Posting hands the floor to the other party, and the result tells you
    the state of the conversation in `floor`:
      - `floor.alone: true` — NOBODY else has spoken in this room. Do not wait for a
        reply that cannot come; say so, or do the work yourself.
      - `floor.anyone_listening: true` — someone is long-polling right now, so a reply
        is actually coming and waiting is the right move.
      - `floor.is_you: true` on a read means it is YOUR turn to speak; nobody else will.
      - `floor.stalled: true` — the others were here and have gone quiet. Not the
        same as `alone`, and not a reason to keep waiting.
    Pass `speaker` once to name yourself ("mac", "windows-engram") — it labels you in
    the transcript, which otherwise cannot tell two of your own sessions apart. With
    three or more the floor rotates to whoever has spoken least recently; use
    `hand_to` to name someone specific instead.

    WHEN ONLY THE PERSON CAN DECIDE, pass `ask_human` with the question. It blocks the
    room on them, takes the floor away from every agent (so nobody sits waiting on a
    session that is itself waiting), notifies the user, and carries the question to
    anyone who looks. Their reply unblocks it and hands the floor back to you. Use it
    for real decisions — scope, spend, anything irreversible — not to avoid work.
    Any session that sees `awaiting_human` should put the question to the user in
    chat and relay their answer with kb_room_relay_answer — do not assume they are
    watching a web page.

    PASS `expect_cursor` — the cursor from the read you are replying to. Composing a
    turn takes you tens of seconds, and the room moves while you write: every session
    in the first live test asserted something about the room that had stopped being
    true, including claiming a member wasn't present who had been for 24 minutes. Your
    post always goes through; if turns landed while you were writing, the result tells
    you so and hands them to you in `missed`. Read those before acting on your own
    message.

    `floor.do_next` is one sentence saying what to do — prefer it to re-deriving the
    same conclusion from the flags.

    KEEP TURNS SHORT — a claim plus a pointer, not a document. A turn is capped at
    4000 chars, but the real limit is attention and tokens: every member pays for
    every word, and a long turn is read once and lost. When you have something
    substantial (a design, a report, an analysis), kb_write it as a concept and pass
    its path in `refs` — the room carries the link and a one-line summary. Shared
    that way it is versioned, searchable, readable on demand and re-readable later;
    pasted into a turn it is none of those. Members read refs with kb_read (their own
    brain) or kb_room_fetch (yours, if you granted the path).

    Respect the room's goal and exit condition: if the goal is met, say so and call
    kb_room_close instead of another agreeable turn. Posts are refused past the turn
    budget (extend with kb_room_extend only if genuinely converging) and absolutely
    refused at the hard cap.

    Returns {turn, replies: [...], floor: {...}} — replies filled when wait_for_reply
    caught turns.
    """
    me = _require_room_user()
    r = _room_of(room)
    _room_scan(message, "room message")
    _rate_limit_post()
    key = _speaker_key()
    if key:
        registry.rooms.touch_speaker(r.id, key, me.id, name=speaker)
        registry.rooms.clear_empty_waits(r.id, key)
    # What landed while you were composing. Captured BEFORE our own write so our
    # turn can't appear in its own missed list.
    missed = []
    if expect_cursor > 0:
        missed = [
            _turn_view(t, registry.tenancy_handle_map())
            for t in registry.rooms.read_turns(r.id, me.id, since_id=expect_cursor)
            if t.kind != "guest_read"
        ]
    turn = registry.rooms.post_turn(
        r.id, me.id, message, session=(f"claude:{key}" if key else "claude"),
        refs=refs, speaker=key, hand_to=(hand_to or None) if hand_to else None,
    )
    if ask_human.strip():
        registry.rooms.ask_human(r.id, ask_human, asker=key)
        # The whole point is that the person finds out. A block nobody is told
        # about is just a quieter version of everyone waiting.
        _push_notification(
            me.id, "room_question",
            f"Room '{r.name}' needs you: {ask_human.strip()[:160]}",
            ref=r.name,
        )
    await _room_notify(r.id)
    handles = registry.tenancy_handle_map()
    # The soft limit is where behaviour actually changes: a long turn still
    # posts (never block the human's intent), but the result teaches the better
    # shape — same nudge pattern as the publish reflex.
    from engram_server.teamwork import _LONG_TURN_CHARS

    warnings: list[str] = []
    if len(message) > _LONG_TURN_CHARS and not refs:
        warnings.append(
            f"That turn was {len(message)} chars. Every member pays for every word, and a "
            "long turn is read once and lost. Next time, kb_write the substance as a "
            "concept and post a one-line summary with its path in `refs` — versioned, "
            "searchable, re-readable, and cheap to skip."
        )
    floor = registry.rooms.floor_state(r.id, key)
    # The single most expensive failure in a room is waiting for a reply that
    # cannot arrive. Say so BEFORE the caller decides to wait, and say which of
    # the three reasons it is — they need different responses from the agent.
    hopeless = ""
    if floor["alone"]:
        hopeless = (
            "Nobody else has spoken in this room, so there is no one to reply. Don't "
            "wait — carry on alone and post what you find, or tell the user the other "
            "session never joined."
        )
    elif floor["stalled"]:
        hopeless = (
            "The others were here but have gone quiet. Don't wait — do the next useful "
            "thing and leave your turn in the room for whenever they come back."
        )
    elif floor["awaiting_human"] and not ask_human.strip():
        hopeless = (
            "This room is blocked on the person, not on another agent: "
            f"{floor['awaiting_human']!r}. No session will answer that. Wait for them, "
            "or get on with work that doesn't depend on it."
        )
    if hopeless:
        warnings.append(hopeless)

    # THE DELIVERED VERDICT (from the PARK pattern playbook in the brain). A parked
    # session gets this turn instantly off the long-poll bus. A session that has
    # GONE cannot be woken by any amount of waiting, so the only honest move is to
    # reach the person, whose tray is still live.
    #
    # Deliberately NOT part of the `hopeless` if/else: telling the caller not to
    # wait and telling the human they are needed are independent, and the case
    # where BOTH apply — everyone has left — is exactly when the notification
    # matters most. An earlier version chained them and so stayed silent in the
    # one situation it was built for.
    #
    # The threshold is GONE, not "not parked at this instant". Two agents in a live
    # conversation are between polls most of the time; notifying on that produced
    # 22 unread pings in one afternoon, five of them for a single turn, which
    # trains the user to ignore notifications and destroys the only channel that
    # reaches them when no session can. A quiet channel is the feature.
    #
    # One notification per PERSON: several sessions of one user are one tray.
    gone = [o for o in floor["others"] if not o["live"] and not o["listening"]]
    for uid in dict.fromkeys(int(o["user_id"]) for o in gone):
        asleep = [o["name"] for o in gone if int(o["user_id"]) == uid]
        _push_notification(
            uid, "room_turn",
            f"Room '{r.name}' — a turn is waiting for {', '.join(asleep)}: "
            f"{message[:120]}",
            ref=r.name,
        )
    replies: list[dict[str, Any]] = []
    if wait_for_reply and not hopeless:
        window = _wait_window(wait_seconds)
        deadline = asyncio.get_event_loop().time() + window
        if key:
            registry.rooms.touch_speaker(
                r.id, key, me.id, listening_seconds=window + _LISTEN_GRACE_S
            )
        while not replies and asyncio.get_event_loop().time() < deadline:
            remaining = int(deadline - asyncio.get_event_loop().time()) or 1
            await _room_wait(r.id, remaining)
            fresh = registry.rooms.read_turns(r.id, me.id, since_id=turn.id)
            # Any turn newer than the one we just wrote is someone else's: this
            # call is blocked here and cannot have posted again. Filtering by
            # user_id (as this did) silently broke the common case — two sessions
            # of ONE person share a user_id, so every reply was discarded and the
            # wait always timed out empty. Guest-read audit rows aren't replies.
            replies = [_turn_view(t, handles) for t in fresh if t.kind != "guest_read"]
        if replies and key:
            registry.rooms.stop_listening(r.id, key)
        floor = registry.rooms.floor_state(r.id, key)
    floor = await _with_working(floor)
    out: dict[str, Any] = {
        "turn": _turn_view(turn, handles), "replies": replies, "floor": floor,
    }
    if missed:
        out["missed"] = missed
        warnings.append(
            f"{len(missed)} turn(s) landed while you were composing — they are in "
            "`missed`. Read them before acting on what you just said; it may already "
            "be answered, contradicted, or moot."
        )
    if key and not speaker and not any(
        s["speaker"] == key and s["name"] != key for s in floor["speakers"]
    ):
        # Unnamed sessions cannot survive a reconnect: the key dies with the
        # connection, so they come back as a stranger and the room ends up waiting
        # on a "them" that no longer exists.
        warnings.append(
            "You haven't named yourself. Pass speaker='<something-stable>' — without "
            "it, any reconnect (a server restart is enough) makes you a NEW "
            "participant and the room may wait on the version of you that's gone."
        )
    if wait_for_reply and not replies and not hopeless:
        out["waited"] = True
        out["next"] = {
            "tool": "kb_room_read", "room": room, "since": turn.id,
            "hint": "call this to keep waiting — write nothing in between",
        }
        warnings.append(
            f"No reply within {_wait_window(wait_seconds)}s — a timeout, not a refusal. "
            "They hold the floor, so they know it's their turn; do NOT re-post the same "
            "thing. To keep waiting, call kb_room_read with the cursor in `next` (a "
            "longer wait_seconds would just be killed by the host). Otherwise get on "
            "with something useful and check back."
        )
    if warnings:
        out["warnings"] = warnings
    return out


@mcp.tool()
async def kb_room_read(
    room: str, since: int = 0, wait_seconds: int = 0, speaker: str = ""
) -> dict[str, Any]:
    """Read a room's turns after cursor `since` (turn id). Pass wait_seconds (e.g. 25)
    to long-poll server-side for the next turn instead of polling — free while idle.
    While you long-poll here you are marked LISTENING, so the other side can see a
    reply is actually coming rather than guessing whether you left.

    Check `floor` in the result: `is_you` means it's your turn to speak and nobody
    else will; `alone` means nobody else is in the room at all, so waiting is futile.

    Pass the same `speaker` name you post under. A reconnect (including the server
    restarting) gives you a NEW underlying session, and without the name you'd
    register as a second, separate participant — the room would then wait on a
    "you" that no longer exists. The name is what survives; re-announcing it folds
    the old row back into you.

    Returns {room, turns: [...], cursor, floor}."""
    me = _require_room_user()
    started = time.monotonic()
    r = _room_of(room)
    key = _speaker_key()
    if key and r.status == "open":
        # Reading a room means you are IN the conversation. Registering only on
        # long-poll left the first speaker addressing an empty house — nobody was
        # on record, so the floor opened and the reader arrived to find no
        # obligation. Both sides then waited. Announce presence BEFORE parking,
        # too: a listen recorded after the poll is a promise kept only once it no
        # longer matters.
        registry.rooms.touch_speaker(
            r.id, key, me.id, name=speaker,
            listening_seconds=(
                _wait_window(wait_seconds) + _LISTEN_GRACE_S if wait_seconds > 0 else 0
            ),
        )
    turns = registry.rooms.read_turns(r.id, me.id, since_id=since)
    if not turns and wait_seconds > 0 and r.status == "open":
        await _room_wait(r.id, _wait_window(wait_seconds))
        turns = registry.rooms.read_turns(r.id, me.id, since_id=since)
        if key:
            if turns:
                # Woken early: about to act on what we read, not keep waiting.
                registry.rooms.stop_listening(r.id, key)
                registry.rooms.clear_empty_waits(r.id, key)
            else:
                registry.rooms.note_empty_wait(r.id, key)
    handles = registry.tenancy_handle_map()
    return {
        "room": _room_view(r),
        "turns": [_turn_view(t, handles) for t in turns],
        "cursor": turns[-1].id if turns else since,
        "floor": await _with_working(registry.rooms.floor_state(r.id, key)),
        # How long the SERVER actually held this call. Two sessions independently
        # timed a long-poll by bracketing it with `date` and concluded the server
        # was sitting on turns it already had; a bracket like that spans their own
        # model turn — inference, the call, then more inference — so it cannot
        # separate server wait from client latency. Reporting it from inside the
        # handler ends that argument with a number instead of an inference.
        "server_ms": int((time.monotonic() - started) * 1000),
    }


@mcp.tool()
async def kb_prepare_session(
    project: str,
    task: str,
    repo_path: str,
    files: list[str] | None = None,
    refs: list[str] | None = None,
    goal: str = "",
    exit_condition: str = "",
    base: str = "main",
    name: str = "",
) -> dict[str, Any]:
    """Prepare an isolated session for one chunk of work, and hand back the command
    that starts it. Use when the user wants to split work across sessions, or when a
    piece of work deserves its own branch and its own thread.

    ONE CALL sets up everything a fresh session needs to start cold and start right:
    a git worktree + branch (so parallel chunks cannot touch each other's files), an
    `.engram-project` pin inside it (so the new session's first `realign` resolves
    with no questions), a thread carrying `goal` and `exit_condition` (so it knows
    what done means), advisory claims on `files` (so the chunk shows in everyone
    else's `floor.working` before a keystroke), and a brief concept.

    THE BRIEF IS A MAP, NOT A DUMP. Pass `refs` — paths to the decisions, specs and
    constraints this chunk should know about. They attach as links; the session reads
    what it needs when it needs it. Search the brain for them first: prior decisions
    and the constraints that have already cost time are exactly what a new session
    would otherwise rediscover or contradict.

    `repo_path` is REQUIRED and absolute: the server has its own working directory
    and cannot see yours, so there is nothing sensible to default to.

    Returns {worktree, branch, thread, brief_path, command, warnings}. Give the user
    `command` to run. Only the worktree is a hard failure — if the brain is
    unreachable you still get a working command, with warnings naming what was not
    recorded.
    """
    me = _require_room_user()
    store = await current_store()
    slug = session_prep.slugify(name or task)
    repo = await to_thread.run_sync(lambda: session_prep.require_repo(repo_path))
    dest = repo.parent / f"{repo.name}-{slug}"

    warnings: list[str] = []
    existing = await to_thread.run_sync(lambda: session_prep.worktree_paths(repo))
    reused = str(dest.resolve()) in existing
    if not reused:
        # The one hard stop. A chunk with no claim is merely uncoordinated; a chunk
        # with no worktree is a collision waiting to happen.
        await to_thread.run_sync(
            lambda: session_prep.add_worktree(repo, dest, slug, base)
        )

    # Pin: without it the new session must be told its project, which is precisely
    # the hand-holding this tool exists to remove.
    try:
        await to_thread.run_sync(
            lambda: (dest / ".engram-project").write_text(
                f"{project.strip()}\n", encoding="utf-8"
            )
        )
    except OSError as exc:
        warnings.append(
            f"Could not write .engram-project ({exc}) — the new session will need the "
            "project named on its first realign."
        )

    brief_path = f"projects/{project.strip()}/briefs/{slug}.md"
    ref_lines = "\n".join(f"- [{Path(r).stem}]({_rel_link(brief_path, r)})" for r in (refs or []))
    body = (
        f"# {task.strip()}\n\n"
        + (f"**Goal.** {goal.strip()}\n\n" if goal.strip() else "")
        + (f"**Done when.** {exit_condition.strip()}\n\n" if exit_condition.strip() else "")
        + f"**Worktree.** `{dest}` on branch `{slug}`.\n\n"
        + (f"## Read first\n\n{ref_lines}\n\n" if ref_lines else "")
        + "## Notes\n\nWritten by kb_prepare_session. Read the linked concepts before "
        "starting — they carry decisions already made and constraints already paid for.\n"
    )
    try:
        await store.kb_write(
            brief_path,
            f"---\ntype: note\ndescription: Brief for {task.strip()[:120]}\n---\n\n{body}",
            f"brief: {slug}",
        )
    except Exception as exc:  # noqa: BLE001 — the worktree is what matters
        warnings.append(f"Brief not written ({exc}).")
        brief_path = ""

    try:
        await kb_thread_post(
            slug, "prepare",
            f"Chunk prepared: {task.strip()}\n\nWorktree `{dest}` on `{slug}`.",
            topic=task.strip()[:120], goal=goal, exit_condition=exit_condition,
            refs=([brief_path] + list(refs or [])) if brief_path else list(refs or []),
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Thread not opened ({exc}) — coordinate manually.")

    for f in (files or []):
        try:
            await store.kb_claim(slug, f, note=task.strip()[:80])
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Claim on {f} failed ({exc}).")

    if reused:
        warnings.append(
            f"Worktree {dest} already existed — reused rather than recreated. If that "
            "is stale work, remove it with `git worktree remove` first."
        )
    return {
        "worktree": str(dest),
        "branch": slug,
        "thread": slug,
        "brief_path": brief_path,
        "command": f'cd "{dest}" && claude',
        "first_message": "realign",
        "warnings": warnings,
    }


@mcp.tool()
async def kb_finish_session(
    thread: str,
    project: str,
    repo_path: str = "",
    summary: str = "",
    pr_title: str = "",
    pr_body: str = "",
    base: str = "main",
) -> dict[str, Any]:
    """Close out a chunk: gather the proof of work, log it, close the thread, release
    the claims. Call when the work is done — before you end the session, not after.

    IT DETECTS WHAT HAPPENED rather than being told, so you cannot misclassify the
    chunk: commits on the branch (with links), and concepts written in the brain
    since the thread was created. A chunk with NO commits is not a failure — that is
    a research chunk, and its concepts ARE the proof of work.

    NOTHING IS OPENED ON YOUR BEHALF. If you pass `pr_title`/`pr_body` they are put in
    front of the user via ask_human and the thread blocks until they answer. Their
    decision is relayed back; you open the PR only if they said yes. Hiren's record
    notes two PRs opened without asking that had to be closed.

    Claims are released even when the steps above fail — a stale claim outlives the
    session and misinforms everyone.

    Returns {commits, concepts, logged, thread_closed, claims_released, awaiting_human}.
    """
    me = _require_room_user()
    store = await current_store()
    result: dict[str, Any] = {
        "commits": [], "concepts": [], "logged": False,
        "thread_closed": False, "claims_released": 0, "warnings": [],
    }

    started = ""
    try:
        info = await store.kb_thread_read(thread, None, 0)
        turns = info.get("turns") or []
        started = str(turns[0]["timestamp"]) if turns else ""
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(f"Could not read the thread ({exc}).")

    if repo_path.strip():
        try:
            repo = await to_thread.run_sync(
                lambda: session_prep.require_repo(repo_path)
            )
            result["commits"] = await to_thread.run_sync(
                lambda: session_prep.commits_on(repo, thread, base)
            )
            result["repo"] = await to_thread.run_sync(
                lambda: session_prep.remote_slug(repo)
            )
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"Could not read commits ({exc}).")

    if started:
        try:
            result["concepts"] = await store.kb_concepts_since(started)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"Could not list concepts ({exc}).")

    # The log entry is the durable record, and proof-links are what make "shipped X"
    # checkable rather than asserted.
    body = summary.strip() or f"Finished chunk {thread}."
    if result["concepts"]:
        body += "\n\nWritten: " + ", ".join(result["concepts"][:10])
    if not result["commits"] and result["concepts"]:
        body += "\n\nNo commits — the output of this chunk is the concepts above."
    try:
        await store.kb_append_log(
            project, body,
            commits=[c["sha"] for c in result["commits"]] or None,
            repo=result.get("repo") or "",
        )
        result["logged"] = True
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(f"Log not appended ({exc}).")

    if pr_title.strip():
        # Blocks the thread on the person and pushes a notification. Whichever
        # session is talking to them relays the answer back.
        try:
            # A THREAD, not a room — prepare creates threads, so ask_human rides on
            # kb_thread_post. Calling kb_room_post here failed on the room lookup
            # every time, which the tests caught before it ever ran for real.
            await kb_thread_post(
                thread, "finish",
                f"Chunk done. Proposed PR:\n\n**{pr_title.strip()}**\n\n{pr_body.strip()}",
                ask_human=(
                    f"Open this PR? Title: {pr_title.strip()}. "
                    "Nothing is opened until you say so."
                ),
            )
            result["awaiting_human"] = pr_title.strip()
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"Could not put the PR to the user ({exc}).")

    # Release claims BEFORE closing, and outside the success path — a stale claim
    # outlives the session and misinforms everyone.
    try:
        for c in await store.kb_claims():
            if str(c.get("session") or "") == thread and not c.get("stale"):
                await store.kb_release(thread, str(c["path"]))
                result["claims_released"] += 1
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(f"Claims not fully released ({exc}).")

    if not result.get("awaiting_human"):
        try:
            await kb_thread_post(
                thread, "finish", body, close=True,
            )
            result["thread_closed"] = True
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"Thread not closed ({exc}).")
    else:
        result["warnings"].append(
            "Thread left OPEN because it is waiting on the user's PR decision — "
            "close it once they have answered."
        )
    return result


def _rel_link(from_path: str, to_path: str) -> str:
    """A relative markdown link between two brain concepts, so the graph stays
    walkable from the brief."""
    try:
        return posixpath.relpath(to_path, posixpath.dirname(from_path))
    except ValueError:
        return to_path


@mcp.tool()
async def kb_room_relay_answer(room: str, answer: str) -> dict[str, Any]:
    """Pass the USER'S OWN ANSWER into a room that is blocked on them (ask_human).

    WHEN: a room shows `awaiting_human`, you told the user the question in chat,
    and they answered you. Call this with THEIR answer, in their words. It clears
    the block and hands the floor back to whoever asked, exactly as if they had
    typed it in the room.

    WHY THIS EXISTS: the question reaches the user fine, but answering it used to
    mean opening the dashboard — somewhere they rarely are. So a room could sit
    blocked for hours on a person who had already made the decision, out loud, in
    a conversation with you. Ask them where they already are, and relay.

    NEVER call this with anything the user did not actually say. Not your summary
    of what they'd probably want, not an inference from earlier context, not a
    decision you think is obvious. Other sessions will act on this believing a
    human decided it, and that belief is the only thing making it worth more than
    your own opinion. If they haven't answered yet, ask them and wait.

    Works for a ROOM or a THREAD — pass either name. Threads keep their transcript in
    git, so nothing there passes through the room tables to notice the answer; without
    handling both, ask_human on a thread would block it forever.

    The turn is marked `relayed` — their words, passed through you, never a claim
    that they were in the room. Returns {ok, turn, floor}."""
    me = _require_room_user()
    if not answer.strip():
        raise KBError("Nothing to relay — ask the user the question first.")
    _room_scan(answer, "relayed answer")
    _rate_limit_post()
    key = _speaker_key()

    existing = registry.rooms.room_by_name(room.strip().lower())
    if existing is None:
        # Not a room — try it as a thread, whose floor lives in a shadow room.
        # Confirm the thread EXISTS first: the shadow room is created on demand,
        # so a typo would otherwise invent a brand-new thread whose only content
        # is the user's answer to a question nobody asked.
        probe = await (await current_store()).kb_thread_read(room, None, 0)
        if probe.get("status") == "none":
            raise KBError(
                f"No room or thread named '{room}'. kb_rooms() lists your rooms, "
                "kb_threads() your threads."
            )
        fid = _thread_floor_id(room)
        if fid is None:
            raise KBError(f"Cannot reach the floor state for thread '{room}'.")
        out = await (await current_store()).kb_thread_post(
            room, me.handle, answer, False, "", "", "", None, False, False, 0
        )
        registry.rooms.answer_human(fid)
        return {
            "ok": True,
            "turn": {"sender": me.handle, "message": answer, "via": "human",
                     "relayed": True, "seq": out.get("seq")},
            "floor": await _with_working(registry.rooms.floor_state(fid, key)),
        }

    turn = registry.rooms.post_turn(
        existing.id, me.id, answer, session=f"relay:{key or 'x'}",
    )
    await _room_notify(existing.id)
    return {
        "ok": True,
        "turn": _turn_view(turn, registry.tenancy_handle_map()),
        "floor": await _with_working(registry.rooms.floor_state(existing.id, key)),
    }


@mcp.tool()
async def kb_room_invite(room: str, handle: str) -> dict[str, Any]:
    """Invite another Engram user into an open room you are in. They get a notification
    (Chrome extension + email). Returns {invited: bool}."""
    me = _require_room_user()
    r = _room_of(room)
    other = registry.tenancy.user_by_handle(handle.lstrip("@").lower())
    if other is None:
        raise KBError(f"No Engram user @{handle.lstrip('@')}. kb_explore() lists people.")
    added = registry.rooms.invite(r.id, me.id, other.id)
    if added:
        _push_notification(
            other.id, "room_invite",
            f"@{me.handle} invited you to room '{r.name}': {r.goal[:120]}",
            ref=r.name,
        )
    return {"invited": added}


@mcp.tool()
async def kb_room_grant(room: str, path: str) -> dict[str, Any]:
    """Grant the members of an open room read+search access to a PATH PREFIX of your
    brain (e.g. 'projects/slate') for the life of the room. Auto-revoked on close;
    every access is logged as a visible turn. Confirm with the user first — anything
    read may be copied. Never grant broad prefixes like 'projects'.
    Returns {granted: path}."""
    me = _require_room_user()
    r = _room_of(room)
    registry.rooms.add_grant(r.id, me.id, path)
    handles = registry.tenancy_handle_map()
    t = registry.rooms.post_turn(
        r.id, me.id, f"granted room access to '{path}'", kind="system"
    )
    await _room_notify(r.id)
    return {"granted": path, "turn": _turn_view(t, handles)}


@mcp.tool()
async def kb_room_search(room: str, owner: str, query: str) -> dict[str, Any]:
    """Search a fellow room member's GRANTED slice of their brain — the live-join
    superpower: mid-conversation, pull the exact decision out of their work instead of
    asking them to remember it. Only paths they granted to THIS room are searchable;
    the search is logged in the room as an audit turn. Read a specific hit with
    kb_room_fetch. Returns {results: [...]}."""
    me = _require_room_user()
    r = _room_of(room)
    _require_member(r, me)
    owner_user = registry.tenancy.user_by_handle(owner.lstrip("@").lower())
    if owner_user is None:
        raise KBError(f"No Engram user @{owner.lstrip('@')}.")
    grants = [g for g in registry.rooms.grants_for(r.id) if g["grantor_id"] == owner_user.id]
    if not grants:
        raise KBError(
            f"@{owner_user.handle} has granted nothing to this room. They can with "
            "kb_room_grant('{room}', 'projects/<their-project>')."
        )
    store = await registry.store_for_handle(owner_user.handle)
    raw = await store.kb_search(query, limit=20)
    results = []
    for hit in raw or []:
        for g in grants:
            try:
                registry.rooms.assert_grant(r.id, owner_user.id, hit["path"])
                results.append(hit)
                break
            except Exception:  # noqa: BLE001 — not covered by this grant, try next
                continue
    t = registry.rooms.post_turn(
        r.id, me.id,
        f"searched @{owner_user.handle}'s granted work for '{query[:80]}' ({len(results)} hits)",
        kind="guest_read", session="claude",
    )
    await _room_notify(r.id)
    return {"results": results[:12], "turn": t.id}


@mcp.tool()
async def kb_room_fetch(room: str, owner: str, path: str) -> dict[str, Any]:
    """Read ONE concept from a fellow room member's granted slice (path must sit under
    a prefix they granted to this room). Logged as an audit turn. Returns the concept
    like kb_read (no depth)."""
    me = _require_room_user()
    r = _room_of(room)
    _require_member(r, me)
    owner_user = registry.tenancy.user_by_handle(owner.lstrip("@").lower())
    if owner_user is None:
        raise KBError(f"No Engram user @{owner.lstrip('@')}.")
    registry.rooms.assert_grant(r.id, owner_user.id, path)
    store = await registry.store_for_handle(owner_user.handle)
    got = await store.kb_read(path)
    t = registry.rooms.post_turn(
        r.id, me.id, f"read @{owner_user.handle}'s {path}", kind="guest_read", session="claude"
    )
    await _room_notify(r.id)
    return {**got, "owner": owner_user.handle, "turn": t.id}


@mcp.tool()
async def kb_room_extend(room: str, extra_turns: int = 20) -> dict[str, Any]:
    """Raise an open room's turn budget (never past its hard cap). Only extend when the
    conversation is genuinely converging on the goal — if it's circling, close instead.
    Returns {turn_budget}."""
    me = _require_room_user()
    r = _room_of(room)
    updated = registry.rooms.extend_budget(r.id, me.id, extra_turns)
    await _room_notify(r.id)
    return {"turn_budget": updated.turn_budget, "hard_cap": updated.hard_cap}


@mcp.tool()
async def kb_room_close(room: str, outcome: str = "") -> dict[str, Any]:
    """Close a room — and PRECIPITATE it. `outcome` should be a 3-10 line synthesis of
    what was decided/learned (write it yourself from the transcript before calling).

    The outcome is only STORED on the room and OFFERED back: present it to the user
    and, if they accept, write it into THEIR brain with kb_write (type: decision or
    note, body ending with 'From room <name>, closed <date>'). Never write it without
    their yes — a room's conclusion is offered, not committed (quarantine principle).
    Every other member's Claude gets the same offer via the close notification.

    Returns {room, precipitate_instruction}.
    """
    me = _require_room_user()
    r = _room_of(room)
    _room_scan(outcome, "room outcome")
    pending = registry.rooms.floor_state(r.id).get("awaiting_human", "")
    closed = registry.rooms.close_room(r.id, me.id, outcome=outcome)
    await _room_notify(r.id)
    handles = registry.tenancy_handle_map()
    for m in registry.rooms.members(closed.id):
        if m["user_id"] != me.id:
            _push_notification(
                m["user_id"], "room_closed",
                f"Room '{closed.name}' closed by @{me.handle}"
                + (f" — outcome: {outcome[:100]}" if outcome else ""),
                ref=closed.name,
            )
    out_warnings: list[str] = []
    if pending:
        # Closing over an unanswered question to the PERSON throws away the one
        # thing in the room that was waiting on them. Closing still succeeds —
        # the caller may know the question is moot — but it must be said out loud.
        out_warnings.append(
            f"This room was still waiting on the user: {pending!r}. That question is "
            "now closed unanswered — put it to them directly if it still matters."
        )
    return {
        "room": _room_view(closed),
        **({"warnings": out_warnings} if out_warnings else {}),
        "precipitate_instruction": (
            "Offer this outcome to the user. If they accept, save it to their brain: "
            "kb_write('projects/<relevant>/decisions/<date>-<slug>.md', ...) with the "
            f"outcome as body and provenance line 'From room {closed.name}, closed "
            f"{closed.closed_at}'. Do NOT write without their explicit yes."
        ),
    }


@mcp.tool(meta=_app_meta)
async def kb_app(view: str = "home") -> dict[str, Any]:
    """Open the Engram app — the one card with everything: Home (projects), Browse
    (search + artifacts), People (directory + presence), Rooms (live rooms + DMs +
    notifications), Office (live floor). Call when the user says "open engram",
    asks for their dashboard/overview in chat, or names a tab ("show the office").
    Pass view to open a specific tab. After it mounts, say ONE short line and stop.

    Returns a compact seed {view, projects, unread} — the app pulls its own data.
    """
    v = view if view in ("home", "browse", "people", "rooms", "office") else "home"
    projects = await (await current_store()).kb_projects()
    return {
        "view": v,
        "projects": len(projects),
        "unread": sum(int(p.get("unread") or 0) for p in projects),
    }


def _rooms_state_payload(me) -> dict[str, Any]:
    handles = registry.tenancy_handle_map()
    rooms = []
    for row in registry.rooms.list_rooms_for(me.id, include_closed=False):
        last = row.get("last_turn")
        rooms.append({
            "id": row["id"], "name": row["name"], "goal": row["goal"],
            "status": row["status"], "turn_budget": row["turn_budget"],
            "hard_cap": row["hard_cap"], "messages_used": row.get("messages_used", 0),
            "member_handles": [handles.get(uid, "?") for uid in row["member_ids"]],
            "unread": row["unread"],
            "last_turn": (
                {"author": handles.get(last["author_id"], "?"), "body": last["body"],
                 "created": last["created"]}
                if last else None
            ),
        })
    return {"me": me.handle, "rooms": rooms}


@mcp.tool(meta=_app_only_meta)
async def rooms_state() -> dict[str, Any]:
    """App-only data plane for the unified app's Rooms tab. Never call directly —
    use kb_rooms / kb_app."""
    me = _require_room_user()
    return _rooms_state_payload(me)


@mcp.tool(meta=_app_only_meta)
async def room_transcript(room: str) -> dict[str, Any]:
    """App-only: one room's full transcript for the Rooms tab. Never call directly."""
    me = _require_room_user()
    r = _room_of(room)
    turns = registry.rooms.read_turns(r.id, me.id, since_id=0)
    handles = registry.tenancy_handle_map()
    view = _room_view(r)
    view["messages_used"] = sum(1 for t in turns if t.kind == "message")
    return {"room": view, "turns": [_turn_view(t, handles) for t in turns]}


@mcp.tool(meta=_app_only_meta)
async def room_reply(room: str, message: str) -> dict[str, Any]:
    """App-only: post a turn from the Rooms tab as the signed-in user. Never call
    directly — the model posts with kb_room_post."""
    me = _require_room_user()
    r = _room_of(room)
    _room_scan(message, "room message")
    _rate_limit_post()
    # The widget composer is the HUMAN typing in claude.ai — not their model.
    turn = registry.rooms.post_turn(r.id, me.id, message, session="app")
    await _room_notify(r.id)
    return {"ok": True, "turn": _turn_view(turn, registry.tenancy_handle_map())}


@mcp.tool(meta=_app_only_meta)
async def team_state() -> dict[str, Any]:
    """App-only: the live team roster for the People tab's 'working now' strip.
    Never call directly — the model uses kb_team."""
    me = _require_user()
    state = _team_state_payload()
    if me is not None:
        mine = registry.presence.self_row(me.id) or {}
        state["me"] = {"handle": me.handle, "invisible": bool(mine.get("invisible"))}
    return state


def _team_state_payload() -> dict[str, Any]:
    """Who's working right now (tool-call-derived presence). Shared by the widget
    data plane, the dashboard, and the extension endpoint."""
    users = {u.id: u for u in registry.tenancy.list_users()}
    team = []
    for row in registry.presence.roster(active_minutes=120):
        u = users.get(row["user_id"])
        if u is None or u.status != "active":
            continue
        team.append({
            "handle": u.handle, "display_name": u.display_name or u.handle,
            "avatar_url": u.avatar_url or "", "project": row["project"],
            "tool": row["tool"], "minutes_ago": row["minutes_ago"],
        })
    return {"team": team}


@mcp.tool()
async def kb_team(invisible: bool | None = None) -> dict[str, Any]:
    """Your team, live — who's working on what right now (presence is derived from
    tool calls; project-level only, never content). Call when the user asks who's
    around / what the team is doing. Pass invisible=True/False to toggle YOUR OWN
    invisible mode (hidden from everyone's roster until turned off).
    Returns {team: [...], me: {invisible}}."""
    me = _require_room_user()
    if invisible is not None:
        registry.presence.set_invisible(me.id, invisible)
    state = _team_state_payload()
    mine = registry.presence.self_row(me.id) or {}
    return {**state, "me": {"handle": me.handle, "invisible": bool(mine.get("invisible"))}}


# ---------------------------------------------------- explore widget (app-only data plane)

_explore_meta = _app_meta  # v3: Explore is the unified app's People tab
_explore_app_meta = explore_app_tool_meta(settings.widget)


@mcp.tool(meta=_explore_meta)
async def kb_explore_card() -> dict[str, Any]:
    """Open the People view — discover people, browse their public work, follow them, and
    ask questions, all inside the chat. Call when the user wants to explore/browse Engram,
    find people, or see what others published. After it mounts, say one short line.

    Returns a COMPACT summary {people, following, feed_items} — the card pulls its own data.
    """
    me = _require_user()
    if me is None:
        return {"people": 0, "following": 0, "feed_items": 0, "note": "single-user mode"}
    directory = await kb_explore()
    feed = await kb_feed(limit=20)
    return {
        "view": "people",  # unified app: this launcher opens the People tab
        "people": len(directory.get("people", [])),
        "following": len(registry.discovery.following(me.id)),
        "feed_items": len(feed.get("items", [])),
    }


@mcp.tool(meta=_explore_app_meta)
async def explore_state() -> dict[str, Any]:
    """App-only data plane for the Explore widget (invisible to the model). Never call
    directly — use kb_explore_card."""
    me = _require_user()
    if me is None:
        return {"me": None, "people": [], "feed": []}
    directory = await kb_explore()
    feed = await kb_feed(limit=20)
    return {
        "me": {"handle": me.handle, "display_name": me.display_name, "avatar_url": me.avatar_url},
        "people": directory.get("people", []),
        "feed": feed.get("items", []),
    }


@mcp.tool(meta=_explore_app_meta)
async def explore_profile(handle: str) -> dict[str, Any]:
    """App-only: one person's profile + their public work. Widget use only."""
    me = current_user()
    return {
        "profile": await _profile_of_handle(handle, me.id if me else None),
        "public_work": await _public_work_of(handle),
    }


@mcp.tool(meta=_explore_app_meta)
async def explore_concept(handle: str, path: str) -> dict[str, Any]:
    """App-only: read a public concept as PLAIN text for the widget (no HTML). Widget use only."""
    data = await kb_read_public(handle, path)
    body = data.get("content") or ""
    doc = fm_split(body)  # strip frontmatter — the card shows prose, not YAML
    return {
        "handle": data["handle"], "path": data["path"], "title": data["title"],
        "type": data["type"], "project": data["project"],
        "text": (doc.body if doc is not None else body),
    }


@mcp.tool(meta=_explore_app_meta)
async def explore_follow(handle: str, follow: bool = True) -> dict[str, Any]:
    """App-only: follow/unfollow from the widget."""
    res = await kb_follow(handle, unfollow=not follow)
    return {"handle": res["handle"], "is_following": res["following"]}


@mcp.tool(meta=_explore_app_meta)
async def explore_ask(handle: str, path: str, question: str) -> dict[str, Any]:
    """App-only: ask a question about someone's public work from the widget."""
    try:
        res = await kb_ask(handle, path, question)
        return {"ok": True, "ask_id": res["ask_id"]}
    except KBError as exc:
        return {"error": str(exc)}


# ------------------------------------------------------------------ context sharing (M3)
#
# The core Engram feature: grant another user SCOPED read access to part of YOUR
# brain, so their Claude reads your shelf directly. Every guest read checks a
# capability (owner, grantee, path, verb) against the OWNER's store — a capability
# NEVER exposes anything outside its granted path prefixes, and guest reads force
# depth=0 so a shared concept's links can't leak an unshared neighbour.


def _covered_prefixes(owner_id: int, grantee_id: int, verb: str) -> list[str]:
    """Union of path prefixes the grantee holds a live `verb` capability for."""
    prefixes: list[str] = []
    for cap in registry.capabilities.list_granted_to(grantee_id, live_only=True):
        if cap.owner_id == owner_id and verb in cap.verbs:
            prefixes.extend(cap.paths)
    return prefixes


@mcp.tool()
async def kb_share_context(with_handle: str, paths: list[str], verbs: list[str] = ["read", "search"], days: int = 30) -> dict[str, Any]:
    """Grant another Engram user scoped read access to part of YOUR brain by @handle.
    Use when the user says to share a project/folder with someone so THEIR AI can read
    it. paths = repo-relative prefixes in your brain (e.g. ['projects/alt']); verbs =
    any of read/search/browse; days = how long before it expires. The grantee is
    notified. Revoke anytime the grant shows in kb_shared_with_me on their side; you
    manage yours implicitly by letting it expire or asking to revoke.

    Returns {granted_to, paths, verbs, expires}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Context sharing requires multi-user mode.")
    other = registry.tenancy.user_by_handle(with_handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{with_handle.lstrip('@')}.")
    cap = registry.capabilities.grant(me.id, other.id, paths, verbs, days=days)
    _push_notification(
        other.id, "context_shared",
        f"@{me.handle} shared {', '.join(cap.paths)} with you (kb_guest_read/@{me.handle})",
    )
    return {"granted_to": other.handle, "paths": cap.paths, "verbs": cap.verbs, "expires": cap.expires}


@mcp.tool()
async def kb_request_context(owner_handle: str, paths: list[str], reason: str = "") -> dict[str, Any]:
    """Ask another Engram user for scoped read access to part of THEIR brain.
    Use when the user wants access to someone's project and doesn't have it yet.
    paths = prefixes in the owner's brain you want; reason = a short why. The owner is
    notified and can approve with kb_grant_request.

    Returns {requested_from, paths, status: 'pending'}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Context sharing requires multi-user mode.")
    owner = registry.tenancy.user_by_handle(owner_handle.lstrip("@"))
    if owner is None:
        raise KBError(f"No Engram user @{owner_handle.lstrip('@')}.")
    req = registry.capabilities.create_request(me.id, owner.id, paths, reason)
    _push_notification(
        owner.id, "context_request",
        f"@{me.handle} requests access to {', '.join(req.paths)}"
        + (f" — {reason}" if reason else ""),
    )
    return {"requested_from": owner.handle, "paths": req.paths, "status": req.status}


@mcp.tool()
async def kb_grant_request(from_handle: str, approve: bool = True, verbs: list[str] = ["read", "search"], days: int = 30) -> dict[str, Any]:
    """Approve (or deny) a pending context-access request someone sent YOU.
    Use when the user decides on a request surfaced by kb_notifications. On approve,
    a capability for the requested paths is minted with the given verbs/expiry and the
    requester is notified.

    Returns {from, status: 'approved'|'denied', paths?, expires?}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Context sharing requires multi-user mode.")
    other = registry.tenancy.user_by_handle(from_handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{from_handle.lstrip('@')}.")
    pending = [r for r in registry.capabilities.list_incoming_requests(me.id) if r.requester_id == other.id]
    if not pending:
        raise KBError(f"No pending access request from @{other.handle}.")
    req = pending[0]
    registry.capabilities.resolve_request(req.id, me.id, approve)
    if not approve:
        _push_notification(other.id, "context_denied", f"@{me.handle} declined your access request")
        return {"from": other.handle, "status": "denied"}
    cap = registry.capabilities.grant(me.id, other.id, req.paths, verbs, days=days)
    _push_notification(
        other.id, "context_granted",
        f"@{me.handle} granted you {', '.join(cap.paths)} (kb_guest_read/@{me.handle})",
    )
    return {"from": other.handle, "status": "approved", "paths": cap.paths, "expires": cap.expires}


@mcp.tool()
async def kb_shared_with_me() -> dict[str, Any]:
    """List the brains (and paths) other Engram users have shared with you — what you
    can reach via kb_guest_read / kb_guest_search. Call when the user asks what they
    have access to.

    Returns {grants: [{from, paths, verbs, expires}...]}.
    """
    me = _require_user()
    if me is None:
        return {"grants": [], "note": "single-user mode"}
    handle = registry.tenancy_handle_map()
    grants = []
    for cap in registry.capabilities.list_granted_to(me.id, live_only=True):
        grants.append({
            "from": handle.get(cap.owner_id, str(cap.owner_id)),
            "paths": cap.paths, "verbs": cap.verbs, "expires": cap.expires,
        })
    return {"grants": grants}


@mcp.tool()
async def kb_guest_read(owner_handle: str, path: str) -> dict[str, Any]:
    """Read a concept from ANOTHER user's brain that they've shared with you.
    Use when the user wants to look at a specific file in a shelf @someone shared
    (check kb_shared_with_me for what's available). Only paths covered by a live
    'read' grant are reachable; anything else is refused. Links are not expanded
    (a shared concept can't leak an unshared neighbour).

    Returns {owner, path, content, meta}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Guest reads require multi-user mode.")
    owner = registry.tenancy.user_by_handle(owner_handle.lstrip("@"))
    if owner is None:
        raise KBError(f"No Engram user @{owner_handle.lstrip('@')}.")
    if not registry.capabilities.check(owner.id, me.id, path, "read"):
        raise KBError(
            f"You don't have a 'read' grant covering {path!r} from @{owner.handle}. "
            "Ask for access with kb_request_context."
        )
    owner_store = await registry.store_for_handle(owner.handle)
    result = await owner_store.kb_read(path, depth=0)  # depth 0: never expand to unshared neighbours
    return {"owner": owner.handle, "path": result["path"], "content": result["content"], "meta": result["meta"]}


@mcp.tool()
async def kb_guest_search(owner_handle: str, query: str) -> dict[str, Any]:
    """Search ANOTHER user's brain within what they've shared with you. Use when the
    user asks a question that a shared shelf would answer ('what did @X decide about
    auth?'). Results are limited to paths covered by a live 'search' grant.

    Returns {owner, results: [{path, score, snippet}...]}.
    """
    me = _require_user()
    if me is None:
        raise KBError("Guest search requires multi-user mode.")
    owner = registry.tenancy.user_by_handle(owner_handle.lstrip("@"))
    if owner is None:
        raise KBError(f"No Engram user @{owner_handle.lstrip('@')}.")
    prefixes = _covered_prefixes(owner.id, me.id, "search")
    if not prefixes:
        raise KBError(f"You don't have a 'search' grant from @{owner.handle}.")
    owner_store = await registry.store_for_handle(owner.handle)
    hits = await owner_store.kb_search(query)
    results = hits.get("results", hits) if isinstance(hits, dict) else hits
    covered = [
        h for h in results
        if registry.capabilities.check(owner.id, me.id, h.get("path", ""), "search")
    ]
    return {"owner": owner.handle, "results": covered}


@mcp.tool()
async def kb_send(to_handle: str, path: str) -> dict[str, Any]:
    """Send a concept from YOUR brain to a contact's inbox — a one-time copy with
    provenance, not a live grant. Use when the user says to send / forward a specific
    note or doc to another Engram user. They must be a contact. Their Claude finds it
    in their inbox next session.

    Returns {sent_to, as_path}.
    """
    me = _require_user()
    if me is None:
        raise KBError("kb_send requires multi-user mode.")
    other = registry.tenancy.user_by_handle(to_handle.lstrip("@"))
    if other is None:
        raise KBError(f"No Engram user @{to_handle.lstrip('@')}.")
    if not registry.social.are_contacts(me.id, other.id):
        raise KBError(f"You can only send to a contact — connect with @{other.handle} first.")
    src = await (await current_store()).kb_read(path, depth=0)
    findings = _scan_secrets(src["content"])
    if findings:
        raise KBError("Refusing to send: the concept contains what look like secrets.")
    # Strip the source's own frontmatter so the sent copy has exactly ONE (ours).
    raw = src["content"]
    body_only = raw
    if raw.startswith("---"):
        parts = raw.split("\n---", 1)
        if len(parts) == 2:
            body_only = parts[1].lstrip("\n")
    slug = posixpath.basename(path).removesuffix(".md") or "shared"
    dest = f"inbox/imports/from-{me.handle}-{slug}.md"
    body = (
        f"---\ntype: shared\nshared_by: {me.handle}\n"
        f"adopted_from: brain://{me.handle}/{path}\n"
        f"description: shared by {me.handle}\n---\n\n{body_only}\n"
    )
    recipient_store = await registry.store_for_handle(other.handle)
    await recipient_store.kb_write(dest, body, f"chore: shared from @{me.handle}")
    _push_notification(other.id, "shared_concept", f"@{me.handle} sent you {slug} (in your inbox)", ref=dest)
    return {"sent_to": other.handle, "as_path": dest}


# ------------------------------------------------------------------ prompts
# One-tap workflows for claude.ai (the host surfaces these as runnable prompts).


@mcp.prompt()
def daily_briefing() -> str:
    """Morning briefing across the whole brain: messages first, project states, today's focus."""
    return (
        "Give me my Engram briefing — composed LIVE (there is no scheduled briefing "
        "artifact anymore). Call kb_projects first. Surface unread messages FIRST: for "
        "every project with unread_messages > 0, kb_load it and present each message "
        "(title, what it asks, priority; act or ask, then kb_mark_read). In multi-user, "
        "add the TEAM layer next: kb_rooms() (open rooms needing a turn or a close), "
        "kb_notifications() (invites, questions, answers), and kb_feed() (one line only "
        "if a followed teammate shipped something relevant). Then one line per active "
        "project: current state + top open loop (use kb_projects data; kb_load at most "
        "the 1-2 projects that look hot — navigate, never ingest). Flag anything stale "
        "(last_session older than ~2 weeks). Close with a proposed top-3 focus for today "
        "as a short list. A short NARRATIVE, not a data dump — whole briefing under 20 "
        "lines."
    )


@mcp.prompt()
def garden_brain() -> str:
    """Monthly brain gardening — tend the rot the nightly reconcile only reports."""
    return (
        "Let's garden my brain — a maintenance session, ~10 minutes, decisions are mine. "
        "Method: (1) kb_read('library/reports/brain-health.md') — the nightly reconcile's "
        "findings. (2) ORPHANS: for each orphan concept, propose ONE of: link it from the "
        "concept it obviously relates to (kb_edit a 'Related' line there), fold its content "
        "into a better home, or delete it — ask me per item, batch my answers. (3) STALE "
        "PROJECTS (no session in 30+ days): propose archive vs revive; archiving = note in "
        "its context.md + status:archived. (4) INBOX DEBT: kb_inbox items older than a "
        "week — file each into a project or drop it. (5) VISIBILITY AUDIT: kb_public() — "
        "anything exposed that shouldn't be? (6) Close with kb_append_log to the engram "
        "project: one line on what was tended. Keep it moving — this is weeding, not "
        "replanting."
    )


@mcp.prompt()
def ask_brain(question: str = "") -> str:
    """Answer a question FROM the knowledge base with cited sources — search, read, synthesize."""
    q = f"'{question}'" if question else "the question I ask next"
    return (
        f"Answer {q} FROM MY KNOWLEDGE BASE, not from general knowledge. Method: "
        "kb_search it (semantic — try a second phrasing if the first comes back thin); "
        "kb_read the top 2-4 hits, depth=1 on whichever looks central and follow ONE "
        "more link only if it clearly completes the answer; then synthesize a direct, "
        "concise answer. CITE the exact concept paths you used at the end. If the brain "
        "genuinely doesn't contain the answer, say so plainly — and once we settle the "
        "answer in conversation, offer to kb_write it so next time it does."
    )


@mcp.prompt()
def close_session(project: str = "") -> str:
    """Close out the current work session properly — log entry, context update, handoff."""
    target = f"the '{project}' project" if project else "the project we worked on this session"
    return (
        f"Run the Engram session close-out for {target}: (1) DRAFT the log entry — what "
        "happened, decisions made with links to any new concepts, open threads — and SHOW "
        "it to me BEFORE calling kb_append_log; (2) update the project's context.md "
        "(Current Phase / Open Loops / Next Actions) via kb_write; (3) ask if anything "
        "should be told to the next session directly and kb_leave_message if yes; "
        "(4) confirm in one line what was committed."
    )


@mcp.prompt()
def build_artifact(project: str = "", ask: str = "") -> str:
    """Build a polished document from knowledge-base concepts and offer to save it back."""
    scope = f" from the '{project}' project" if project else ""
    want = ask or "the document I describe"
    return (
        f"Help me build {want}{scope}. Pick the source concepts WITH me: use kb_load's "
        "index_tree and kb_search to propose candidate paths, confirm the set, then "
        "kb_read each one. Build it as a proper ARTIFACT (side-panel document, never "
        "chat text) with clear hierarchy and rich formatting; cite source paths in a "
        "small footer. Then OFFER to save it into the brain: kb_write to "
        "projects/<project>/artifacts/YYYY-MM-<slug>.md with frontmatter type: artifact, "
        "sources: [the exact paths], instruction: <what I asked for> — the server stamps "
        "build provenance, and it appears in the Navigator's Artifacts tab, the explorer "
        "gallery at /brain/artifacts, and kb_artifacts."
    )


@mcp.prompt()
def rebuild_artifact(path: str = "") -> str:
    """Rebuild a saved artifact from its stored recipe against the CURRENT brain (living documents)."""
    target = f"'{path}'" if path else "the artifact I name (kb_artifacts lists them)"
    return (
        f"Rebuild {target} from its recipe. kb_read the artifact: its frontmatter carries "
        "sources (the exact concept paths) and instruction (what it was built to be) — "
        "that IS the recipe. kb_read every source at its CURRENT state, then rebuild the "
        "document per the instruction at the same craft standard (proper ARTIFACT, never "
        "chat text). Present it, then tell me briefly WHAT CHANGED versus the stored "
        "version. Offer to save over the SAME path with kb_write (same frontmatter shape; "
        "the server re-stamps built_from) — git keeps every previous version, so this is "
        "how living documents stay current."
    )


# ------------------------------------------------------------------ routes

if _AUTH_ENABLED:

    @mcp.custom_route(settings.oauth_callback_path, ["GET"])
    async def oauth_callback(request: Request) -> Response:
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        async with httpx.AsyncClient() as http:
            try:
                redirect = await handle_callback(_provider, code=code, state=state, http=http)
            except LoginNotAllowedError as exc:
                # MUST precede the generic handlers: names the login, states policy.
                msg = (
                    f"403 Forbidden: '{exc.login}' has no Engram account yet. "
                    f"Create one at {settings.public_url}/join (it's free), then reconnect."
                    if settings.multiuser
                    else f"403 Forbidden: '{exc.login}' is not on the allowlist. "
                    "This server is private — only Hiren's accounts may connect."
                )
                return PlainTextResponse(msg, status_code=403)
            except ValueError:
                return PlainTextResponse("invalid or expired login state", status_code=400)
            except (httpx.HTTPError, Exception):  # noqa: B014 — mirror Survey's callback
                return PlainTextResponse(
                    "login failed: could not complete sign-in with the identity provider",
                    status_code=400,
                )
        return RedirectResponse(redirect)


# Public homepage (M1.2) — registered BEFORE the explorer because both bind GET "/"
# and Starlette matches the first route added. In multiuser the homepage owns "/";
# single-user keeps the explorer's host-based root_redirect (this is a no-op then),
# so Hiren's current deployment is unchanged.
if settings.multiuser:
    register_homepage(mcp, settings)

async def _share_resolver(token: str):
    """Which brain owns a public /share/<token>? Indexed tenant shares -> that tenant's
    brain; anything not indexed (legacy owner shares from single-user) -> the owner brain."""
    handle = registry.capabilities.resolve_public_share(token)
    if handle is None:
        return registry.owner.root
    try:
        return (await registry.store_for_handle(handle)).root
    except KBError:
        return None


# The Chrome extension, downloadable from the product itself (no 'ask your admin
# for a folder'). Zipped live from the repo checkout, cached by newest mtime;
# PUBLIC on purpose — it's client code with no secrets (auth happens via OAuth
# after install), and a teammate needs it BEFORE they can sign in to anything.
_EXT_DIR = Path(__file__).resolve().parents[2] / "clients" / "chrome-extension"
_ext_zip_cache: dict[str, bytes] = {}


@mcp.custom_route("/downloads/engram-chrome-extension.zip", ["GET"])
async def extension_zip(request: Request) -> Response:
    import io as _io
    import zipfile

    if not _EXT_DIR.is_dir():
        return PlainTextResponse("Extension not bundled on this server.", status_code=404)
    files = sorted(p for p in _EXT_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    stamp = str(max((p.stat().st_mtime_ns for p in files), default=0))
    if stamp not in _ext_zip_cache:
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, f"engram-chrome-extension/{p.relative_to(_EXT_DIR).as_posix()}")
        _ext_zip_cache.clear()
        _ext_zip_cache[stamp] = buf.getvalue()
    return Response(
        _ext_zip_cache[stamp],
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=engram-chrome-extension.zip"},
    )


# The Engram skill, downloadable for claude.ai's custom-skill upload (Settings →
# Customize → Skills → Upload). Zip root = the `engram/` folder holding SKILL.md,
# exactly the shape the uploader requires. Same live-zip + cache pattern as the
# extension; public for the same reason.
_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "engram"
_skill_zip_cache: dict[str, bytes] = {}


@mcp.custom_route("/downloads/engram-skill.zip", ["GET"])
async def skill_zip(request: Request) -> Response:
    import io as _io
    import zipfile

    if not _SKILL_DIR.is_dir():
        return PlainTextResponse("Skill not bundled on this server.", status_code=404)
    files = sorted(p for p in _SKILL_DIR.rglob("*") if p.is_file())
    stamp = str(max((p.stat().st_mtime_ns for p in files), default=0))
    if stamp not in _skill_zip_cache:
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, f"engram/{p.relative_to(_SKILL_DIR).as_posix()}")
        _skill_zip_cache.clear()
        _skill_zip_cache[stamp] = buf.getvalue()
    return Response(
        _skill_zip_cache[stamp],
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=engram-skill.zip"},
    )


# ONE WEB APP (Hiren, 2026-08-01): in multi-user, the dashboard IS the human web —
# the explorer's HTML pages are NOT registered at all (no redirects, no second UI;
# the pixel office lives on in the claude.ai widget, workspace/system in the tools).
# /share/* always registers — public share links are host-independent infrastructure.
# Single-user deployments (no dashboard) keep the full explorer: there it IS the UI.
register_explorer(
    mcp, settings, store,
    share_resolver=_share_resolver if settings.multiuser else None,
    human_pages=not settings.multiuser,
)
register_app_widget(mcp, settings)  # v3: the ONE widget every launcher mounts
register_navigator(mcp, settings.widget)
register_meetings_widget(mcp, settings.widget)
register_office_widget(mcp, settings, store, resolver=current_store, launcher_meta=_app_meta)
register_social_widget(mcp, settings.widget)
register_explore_widget(mcp, settings.widget)

# Multi-user dashboard/onboarding (M1.4). The dashboard offers every configured IdP
# for browser sign-in (GitHub for devs, Google for everyone else) — independent of
# settings.oauth_provider, which only picks the MCP connector's IdP. No-op outside
# multiuser.
_dashboard_idps: dict = {}
if settings.github_client_id and settings.github_client_secret:
    _dashboard_idps["github"] = get_idp("github", settings)
if settings.google_client_id and settings.google_client_secret:
    _dashboard_idps["google"] = get_idp("google", settings)
register_dashboard(mcp, settings, registry, _dashboard_idps)


# ------------------------------------------------------------------ entrypoint


def _configure_logging() -> None:
    """Give the root logger a stdout handler so our engram.* module loggers actually
    emit (uvicorn only configures its own loggers; ours propagate to a bare root and
    would otherwise be swallowed). force=True wins over any earlier root config."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def main() -> None:
    _configure_logging()
    log.info("engram: starting (semantic=%s scheduler=%s)", store.semantic is not None, settings.scheduler_enabled)
    if settings.multiuser and not _AUTH_ENABLED:
        # Without OAuth every request resolves to the owner store — one shared
        # brain served to anyone who can reach the port. Never in multiuser.
        raise SystemExit(
            "engram: refusing to start — ENGRAM_MULTIUSER=1 requires OAuth "
            "(IdP client id + secret); unauthenticated multiuser would serve "
            "the owner brain to every caller."
        )
    if settings.multiuser:
        # Bootstrap the operator's account so they're a first-class social user.
        registry.ensure_owner_account()
    if settings.multiuser and len(settings.dashboard_session_secret) < 32:
        # The dashboard signs its browser session cookie with this secret (HS256);
        # a short/empty secret would let anyone forge an onboarding/admin session.
        raise SystemExit(
            "engram: refusing to start — ENGRAM_MULTIUSER=1 requires "
            "ENGRAM_DASHBOARD_SESSION_SECRET of at least 32 characters "
            "(signs the dashboard session cookie). Generate one with e.g. "
            "`python -c \"import secrets;print(secrets.token_urlsafe(32))\"`."
        )
    if settings.dev_no_access and not (
        _public_host.startswith(("localhost", "127.0.0.1"))
        and _explorer_host.startswith(("localhost", "127.0.0.1"))
    ):
        # A tunneled origin sees every request from 127.0.0.1, so a leaked
        # dev bypass would open the explorer to the whole internet.
        raise SystemExit(
            "engram: refusing to start — ENGRAM_DEV_NO_ACCESS=1 with non-localhost "
            "public/explorer URLs would expose the explorer through the tunnel."
        )
    try:
        store.repo.ensure_clone()
    except GitError as exc:
        raise SystemExit(
            f"engram: cannot prepare the brain checkout at {settings.brain_path}: {exc}"
        ) from exc
    try:
        store.repo.pull_rebase()
    except GitError as exc:
        log.warning("engram: initial pull failed, serving local checkout: %s", exc)

    import anyio
    import uvicorn

    async def _serve() -> None:
        # Wrap the Starlette app's own lifespan (FastMCP hardcodes it to the session
        # manager) so the daily scheduler starts ONCE at server startup on the serving
        # loop — that's the loop that owns the store's asyncio.Lock, which scheduled
        # store mutations must run on. FastMCP's own `lifespan=` runs per MCP session,
        # not server-lifetime, so it can't host a background scheduler.
        app = mcp.streamable_http_app()
        inner_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def _lifespan_with_scheduler(app_: Any) -> AsyncIterator[None]:
            scheduler = start_schedulers(store, settings, asyncio.get_running_loop())
            try:
                async with inner_lifespan(app_):
                    yield
            finally:
                if scheduler is not None:
                    scheduler.stop()

        app.router.lifespan_context = _lifespan_with_scheduler
        config = uvicorn.Config(
            app,
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_level=settings.log_level.lower(),
        )
        await uvicorn.Server(config).serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
