"""The live mission-control dashboard: /brain/workspace (roster + rooms + handoffs)
and the thread-turn ``refs`` shared-links row.

The workspace/ tree (presence + handoffs) is written directly on disk here — the
engine that produces it (kb_presence / kb_handoff) is a separate concern. These
tests exercise ONLY the explorer's defensive rendering: the active/stale roster
split, rooms reuse, handoff rows, the meta-refresh, the Access gate, and the
per-turn refs row.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.config import Settings
from engram_server.explorer import register as register_explorer

GITHUB = "https://github.com/metalfinger/engram"


def _client(settings: Settings) -> TestClient:
    mcp = FastMCP("test-workspace")
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


def _open(settings: Settings) -> Settings:
    """Same settings with the Access gate disabled (dev-only bypass)."""
    return settings.model_copy(update={"dev_no_access": True})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_presence(
    brain: Path,
    slug: str,
    *,
    name: str,
    status: str,
    updated: str,
    working_on: str = "Building the workspace board",
    repo: str = "engram",
    branch: str = "main",
    repo_remote: str = GITHUB,
    cwd: str = "C:/code/engram",
    project: str = "engram",
    host: str = "my-pc",
) -> None:
    fm = [
        "---",
        "type: presence",
        f"session: {slug}",
        f"name: {name}",
        f"status: {status}",
        f"working_on: {working_on}",
        f"repo: {repo}",
        f"branch: {branch}",
        f'repo_remote: "{repo_remote}"',
        f'cwd: "{cwd}"',
        f"project: {project}",
        f"host: {host}",
        f"updated: {updated}",
        "---",
        "",
        f"# {name}",
        "",
    ]
    _write(brain / "workspace" / "presence" / f"{slug}.md", "\n".join(fm) + "\n")


def _write_handoff(
    brain: Path,
    fname: str,
    *,
    frm: str,
    to: str,
    summary: str,
    created: str,
    repo: str = "engram",
    branch: str = "feature/workspace",
    state: str = "mid-refactor",
    status: str = "open",
) -> None:
    fm = [
        "---",
        "type: handoff",
        f"from: {frm}",
        f"to: {to}",
        f"summary: {summary}",
        f"repo: {repo}",
        f"branch: {branch}",
        f"state: {state}",
        f"status: {status}",
        "next_steps:",
        "  - Finish the roster card",
        "refs:",
        "  - projects/engram/specs/workspace-coordination.md",
        f"created: {created}",
        "---",
        "",
        "Take it from here.",
        "",
    ]
    _write(brain / "workspace" / "handoffs" / f"{fname}.md", "\n".join(fm) + "\n")


def _write_thread(brain: Path, tid: str, *, refs: tuple[str, ...] = ()) -> None:
    tdir = brain / "threads" / tid
    _write(
        tdir / "thread.md",
        "---\ntype: thread\nstatus: open\ntopic: Deploy handoff\nparticipants:\n"
        "  - alice\n  - bob\n---\n\n# Deploy handoff\n",
    )
    ref_fm = ""
    if refs:
        ref_fm = "refs:\n" + "".join(f"  - {r}\n" for r in refs)
    _write(
        tdir / "turns" / "2026-07-09T120000-alice.md",
        "---\ntype: thread-turn\nsender: alice\ntimestamp: 2026-07-09T12:00:00+00:00\n"
        f"seq: 1\n{ref_fm}---\n\nSharing the spec with you.\n",
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(delta: dt.timedelta) -> str:
    return (_now() + delta).isoformat()


# ------------------------------------------------------------ mission control


def test_workspace_lists_active_session_with_repo_branch_status(settings: Settings) -> None:
    brain = settings.brain_path
    _write_presence(
        brain, "alice-cc", name="Alice-CC", status="working", updated=_iso(dt.timedelta(minutes=-2))
    )
    resp = _client(_open(settings)).get("/brain/workspace")
    assert resp.status_code == 200
    html = resp.text
    assert "Alice-CC" in html
    assert "working" in html  # status label
    assert "engram · main" in html  # repo · branch chip
    assert GITHUB in html  # github origin link
    assert "github" in html  # origin badge
    assert '<meta http-equiv="refresh" content="5">' in html  # auto-refresh
    assert "1 active session" in html


def test_workspace_stale_session_is_collapsed_not_in_roster(settings: Settings) -> None:
    brain = settings.brain_path
    _write_presence(
        brain, "fresh", name="Fresh-One", status="working", updated=_iso(dt.timedelta(minutes=-1))
    )
    _write_presence(
        brain, "old", name="Stale-One", status="idle", updated=_iso(dt.timedelta(hours=-3))
    )
    html = _client(_open(settings)).get("/brain/workspace").text
    assert "Fresh-One" in html and "Stale-One" in html
    # the stale one lives inside the collapsed "Recently seen" <details>, after the fresh card
    assert "Recently seen (1)" in html
    assert html.index("Fresh-One") < html.index("Recently seen")
    assert html.index("Recently seen") < html.index("Stale-One")
    # marked dim
    assert "card wcard stale" in html


def test_workspace_shows_rooms_and_handoffs(settings: Settings) -> None:
    brain = settings.brain_path
    _write_presence(
        brain, "a", name="A", status="working", updated=_iso(dt.timedelta(minutes=-1))
    )
    _write_thread(brain, "deploy-handoff")
    _write_handoff(
        brain,
        "20260709T120000-alice",
        frm="alice",
        to="bob",
        summary="Server half is green, explorer half is yours.",
        created=_iso(dt.timedelta(minutes=-5)),
    )
    html = _client(_open(settings)).get("/brain/workspace").text
    # rooms reuse the thread card + link
    assert "Deploy handoff" in html
    assert "/brain/threads/deploy-handoff" in html
    # handoff row: from → to, summary, link to the file view
    assert "alice" in html and "bob" in html
    assert "Server half is green" in html
    assert "/brain/f/workspace/handoffs/20260709T120000-alice.md" in html
    assert "1 handoff today" in html


def test_workspace_empty_states_when_no_tree(settings: Settings) -> None:
    html = _client(_open(settings)).get("/brain/workspace").text
    assert "No active sessions" in html
    assert "kb_presence" in html
    assert "No rooms yet" in html
    assert "No handoffs yet" in html
    # still auto-refreshes even when empty
    assert '<meta http-equiv="refresh" content="5">' in html


# ------------------------------------------------------------ thread-turn refs


def test_thread_turn_refs_render_as_shared_links(settings: Settings) -> None:
    brain = settings.brain_path
    _write_thread(
        brain,
        "deploy-handoff",
        refs=("projects/engram/specs/workspace-coordination.md",),
    )
    html = _client(_open(settings)).get("/brain/threads/deploy-handoff").text
    assert "Sharing the spec with you." in html
    assert 'class="brefs"' in html
    assert ">shared<" in html
    # the ref renders as a link to the concept, labelled by basename
    assert "/brain/f/projects/engram/specs/workspace-coordination.md" in html
    assert "workspace-coordination.md" in html


# ------------------------------------------------------------ access gate


def test_workspace_route_stays_guarded(settings: Settings) -> None:
    _write_presence(
        settings.brain_path, "a", name="A", status="working", updated=_iso(dt.timedelta(minutes=-1))
    )
    client = _client(settings)  # dev_no_access False → Access gate holds
    assert client.get("/brain/workspace").status_code == 403
