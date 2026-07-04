"""Engram FastMCP app: the 8 kb_* tools + OAuth wall + transparency explorer.

Module-level construction (like the Survey MCP): settings, KBStore, optional
ProxyOAuthProvider, FastMCP instance, tool registration, explorer routes.
``main()`` prepares the brain checkout and serves streamable HTTP.

The tool DOCSTRINGS are the product: for claude.ai sessions (where no skill can
be installed) they carry the entire Engram protocol.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from engram_server.config import Settings, get_settings
from engram_server.errors import GitError
from engram_server.explorer import register as register_explorer
from engram_server.kbstore import KBStore
from engram_server.oauth.idp import get_idp
from engram_server.oauth.provider import LoginNotAllowedError, ProxyOAuthProvider, handle_callback
from engram_server.oauth.store import InMemoryOAuthStore

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


@mcp.tool()
async def kb_projects() -> list[dict[str, Any]]:
    """List all projects in Hiren's knowledge base. Call this when the user asks what
    they're working on, mentions choosing a project, or at the start of a work session
    before any project is identified. Cheap — reads only index files. If no project is
    named yet, ask which one (or infer it from what the user is discussing).

    Returns [{id, title, description, status, last_session, unread_messages}].
    """
    return await store.kb_projects()


@mcp.tool()
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

    Returns {project, context_md, index_tree, recent_log (last 3 entries),
    unread_messages (full bodies), active_concepts (frontmatter only)}.
    """
    return await store.kb_load(project)


@mcp.tool()
async def kb_read(path: str, depth: int = 0) -> dict[str, Any]:
    """Read one concept file from the KB. Use paths discovered via kb_load's index_tree
    or kb_search — never guess paths. Use depth=1 when you need to know what a
    concept's neighbors are before deciding to read them: it adds the frontmatter of
    every concept the file links to (one hop; dangling links come back missing: true).

    Returns {path, content, meta} plus links: [{path, missing, meta}] when depth=1.
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
    links, never wikilinks. The server auto-fills title/description/timestamp (pass
    `description` if the frontmatter lacks one) and on create auto-appends the concept
    to its parent index.md. Filenames kebab-case; decisions as YYYY-MM-slug.md. Paths
    are repo-relative POSIX, e.g. 'projects/alt/decisions/2026-07-search-engine.md'.
    Reserved: index.md and log.md are unwritable here (indexes are server-maintained;
    use kb_append_log for the log), and messages/ only via kb_leave_message.
    context.md IS writable — session close updates it. `message` is the git commit
    message. If the write fails on a conflict, re-read the file, merge intent
    manually, and retry — never overwrite blind.

    Returns {path, created, no_change, sha, pushed, warnings, indexes_updated}.
    """
    return await store.kb_write(path, content, message, description)


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
    proactively — don't let sessions end with unwritten state. Plain entries are
    wrapped as '## YYYY-MM-DD — <first line>' automatically.

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


@mcp.tool()
async def kb_search(
    query: str,
    project: str | None = None,
    type: str | None = None,  # noqa: A002 — tool contract field name
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Search the whole knowledge base by text match over titles, descriptions, tags,
    headings, and body (v1). Use it to find concepts whose paths you don't already
    have — including in projects not currently loaded, and in self/ (Hiren's stack and
    preferences) and library/ (cross-project runbooks): search there before
    reinventing a procedure that likely already exists. Optional filters: project
    (project id) and type (frontmatter type, e.g. 'decision', 'runbook'). Results are
    ranked best-first; follow up with kb_read on the paths.

    Returns [{path, title, description, score, matched_heading}].
    """
    return await store.kb_search(query, project=project, type=type, limit=limit)


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


# ------------------------------------------------------------------ entrypoint


def main() -> None:
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
        print(f"engram: WARNING: initial pull failed, serving local checkout: {exc}", flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
