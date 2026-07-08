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
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from engram_server.config import Settings, get_settings
from engram_server.doctor import run_doctor
from engram_server.errors import GitError
from engram_server.explorer import register as register_explorer
from engram_server.kbstore import KBStore
from engram_server.navigator import navigator_tool_meta, register_navigator
from engram_server.oauth.idp import get_idp
from engram_server.oauth.provider import LoginNotAllowedError, ProxyOAuthProvider, handle_callback
from engram_server.oauth.store import InMemoryOAuthStore
from engram_server.scheduler import start_schedulers

log = logging.getLogger("engram.app")

settings = get_settings()
store = KBStore(settings)


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

if _AUTH_ENABLED:
    _oauth_store = InMemoryOAuthStore(path=settings.oauth_store_path or None)
    _idp = get_idp(settings.oauth_provider, settings)
    _provider = ProxyOAuthProvider(
        store=_oauth_store,
        idp=_idp,
        public_url=settings.public_url,
        callback_path=settings.oauth_callback_path,
        allowed_logins=frozenset(
            login.strip() for login in settings.allowed_logins.split(",") if login.strip()
        ),
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

mcp = FastMCP(
    "engram",
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
_nav_meta = navigator_tool_meta(settings.widget)


@mcp.tool(meta=_nav_meta)
async def kb_projects() -> list[dict[str, Any]]:
    """List all projects in Hiren's knowledge base. Call this when the user asks what
    they're working on, mentions choosing a project, or at the start of a work session
    before any project is identified. Cheap — reads only index files. If no project is
    named yet, ask which one (or infer it from what the user is discussing).

    Returns [{id, title, description, status, last_session, unread_messages}].

    When the Navigator widget mounts from this call, say one short line and let the user drive it.
    """
    return await store.kb_projects()


@mcp.tool(meta=_nav_meta)
async def kb_load(project: str) -> dict[str, Any]:
    """Load a project's working context. Call when the user names a project to work on
    ('load alt', "let's do hyprlocl work"). Returns state + navigation indexes + unread
    inter-session messages — NOT concept bodies; fetch those individually with kb_read
    as needed (navigate, never ingest: a good session touches ~5 files).

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
    """
    return await store.kb_load(project)


@mcp.tool()
async def kb_read(path: str, depth: int = 0) -> dict[str, Any]:
    """Read one concept file from the KB. Use paths discovered via kb_load's index_tree
    or kb_search — never guess paths. Use depth=1 when you need to know what a
    concept's neighbors are before deciding to read them: it adds the frontmatter of
    every concept the file links to (one hop; dangling links come back missing: true)
    AND backlinks — every concept that cites this one — so the graph walks both ways.

    Returns {path, content, meta} plus links + backlinks when depth=1.
    """
    return await store.kb_read(path, depth)


@mcp.tool()
async def kb_write(path: str, content: str, message: str, description: str = "") -> dict[str, Any]:
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
    manually, and retry — never overwrite blind.

    Returns {path, created, no_change, sha, pushed, warnings, indexes_updated, superseded}.
    """
    return await store.kb_write(path, content, message, description)


@mcp.tool()
async def kb_edit(
    path: str,
    operation: str,
    content: str = "",
    find: str | None = None,
    section: str | None = None,
    occurrence: int | str = 1,
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
    kb_write.

    Returns {path, sha, pushed, operation, warnings}.
    """
    return await store.kb_edit(path, operation, content, find, section, occurrence)


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
    return await store.kb_move(old_path, new_path)


@mcp.tool()
async def kb_append_log(project: str, entry: str) -> dict[str, Any]:
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

    Returns {ok, path, sha, date, pushed}.
    """
    return await store.kb_append_log(project, entry)


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
    return await store.kb_leave_message(project, title, body, to, priority, expires)


@mcp.tool()
async def kb_mark_read(message_path: str) -> dict[str, Any]:
    """Archive an inter-session message AFTER acting on it: flips its status to read
    and moves it to messages/archive/. Pass the exact path from kb_load's
    unread_messages. Expired messages get archived too (mention them briefly, don't
    act on them).

    Returns {archived_path, sha, pushed}.
    """
    return await store.kb_mark_read(message_path)


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
    return await store.kb_search(
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
    return await store.kb_inbox(text)


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
    return await store.kb_import(source, payload, dry_run)


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
    return await run_doctor(settings, store)


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
    return await store.kb_rename_project(old_id, new_id)


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
    return await store.kb_artifacts(project)


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
    return await store.kb_recipes(project)


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
    return await store.kb_share_artifact(path, allow_secrets)


@mcp.tool()
async def kb_unshare_artifact(path: str) -> dict[str, Any]:
    """Revoke an artifact's public share link — the URL stops resolving immediately on the
    next push. Use when the user says to unshare, revoke, or make an artifact private again.
    No-op (still succeeds) if it was never shared.

    Returns {path, sha, pushed}.
    """
    return await store.kb_unshare_artifact(path)


# ------------------------------------------------------------------ prompts
# One-tap workflows for claude.ai (the host surfaces these as runnable prompts).


@mcp.prompt()
def daily_briefing() -> str:
    """Morning briefing across the whole brain: messages first, project states, today's focus."""
    return (
        "Give me my Engram briefing. Call kb_projects first. Surface unread messages "
        "FIRST: for every project with unread_messages > 0, kb_load it and present each "
        "message (title, what it asks, priority; act or ask, then kb_mark_read). Then one "
        "line per active project: current state + top open loop (use kb_projects data; "
        "kb_load at most the 1-2 projects that look hot — navigate, never ingest). Flag "
        "anything stale (last_session older than ~2 weeks). Close with a proposed top-3 "
        "focus for today as a short list. Whole briefing under 20 lines."
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
                return PlainTextResponse(
                    f"403 Forbidden: '{exc.login}' is not on the allowlist. "
                    "This server is private — only Hiren's accounts may connect.",
                    status_code=403,
                )
            except ValueError:
                return PlainTextResponse("invalid or expired login state", status_code=400)
            except (httpx.HTTPError, Exception):  # noqa: B014 — mirror Survey's callback
                return PlainTextResponse(
                    "login failed: could not complete sign-in with the identity provider",
                    status_code=400,
                )
        return RedirectResponse(redirect)


register_explorer(mcp, settings)
register_navigator(mcp, settings.widget)


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
