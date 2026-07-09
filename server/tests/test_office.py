"""The living-office backend: /brain/api/office.json, /brain/api/session/{sid}, /brain/office.

These exercise the JSON derivations end-to-end over a REAL git checkout (the ``gitrepo``
fixture clones the brain skeleton), since the activity feed and the dossier timeline are
``git log`` / ``git show`` derived. Files are seeded on disk and committed with the exact
commit-message grammar the server itself uses, so the path->kind and subject->actor maps
are tested against the shapes they will actually meet in production.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.config import Settings
from engram_server.explorer import register as register_explorer
from engram_server.gitops import GitRepo

GITHUB = "https://github.com/metalfinger/engram.git"


def _client(settings: Settings) -> TestClient:
    mcp = FastMCP("test-office")
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


def _open(settings: Settings) -> Settings:
    return settings.model_copy(update={"dev_no_access": True})


def _git(brain: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(brain), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def _commit(brain: Path, rel: str, content: str, message: str) -> None:
    """Write ``rel`` under the checkout and commit it with ``message`` (no push)."""
    target = brain / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    _git(brain, "add", "-A", "--", rel)
    _git(brain, "commit", "-m", message)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _updated(delta_min: float) -> str:
    return (_now() + dt.timedelta(minutes=delta_min)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _presence_doc(
    sid: str,
    *,
    name: str,
    status: str,
    updated: str,
    working_on: str = "Building the office",
    repo: str = "engram",
    branch: str = "main",
    project: str = "engram",
    host: str = "my-pc",
) -> str:
    return "\n".join(
        [
            "---",
            "type: presence",
            f"session: {sid}",
            f"name: {name}",
            f"status: {status}",
            f"working_on: {working_on}",
            f"repo: {repo}",
            f"branch: {branch}",
            f'repo_remote: "{GITHUB}"',
            'cwd: "C:/code/engram"',
            f"project: {project}",
            f"host: {host}",
            f"updated: {updated}",
            "---",
            "",
            f"# {name}",
            "",
        ]
    )


def _thread_files(brain: Path, tid: str, sender: str, message: str) -> None:
    _commit(
        brain,
        f"threads/{tid}/thread.md",
        "---\ntype: thread\nstatus: open\ntopic: Roll call\n"
        f"participants:\n  - {sender}\n  - bob\n---\n\n# Roll call\n",
        f"thread: {tid} {sender} opened",
    )
    _commit(
        brain,
        f"threads/{tid}/turns/2026-07-09T120000-{sender}.md",
        f"---\ntype: thread-turn\nsender: {sender}\ntimestamp: 2026-07-09T12:00:00+00:00\n"
        f"seq: 1\n---\n\n{message}\n",
        f"thread: {tid} {sender} posted",
    )


def _seed_repo(settings: Settings) -> None:
    """Give the checkout a real .git so office_api's git log/show have history."""
    repo = GitRepo(
        path=settings.brain_path,
        remote=settings.brain_remote,
        branch=settings.brain_branch,
        ssh_key=settings.deploy_key_path,
        author=(settings.git_author_name, settings.git_author_email),
        timeout=settings.git_timeout,
    )
    repo.ensure_clone()


# ------------------------------------------------------------ office.json


def test_office_json_shape_and_seeded_session(settings: Settings) -> None:
    _seed_repo(settings)
    brain = settings.brain_path
    _commit(
        brain,
        "workspace/presence/alice-cc.md",
        _presence_doc("alice-cc", name="Alice-CC", status="working", updated=_updated(-2)),
        "workspace: presence alice-cc (working)",
    )
    resp = _client(_open(settings)).get("/brain/api/office.json")
    assert resp.status_code == 200
    body = resp.json()
    assert set(("now", "sessions", "threads", "activity")) <= set(body)
    assert isinstance(body["sessions"], list)
    row = next(s for s in body["sessions"] if s["session"] == "alice-cc")
    assert row["name"] == "Alice-CC"
    assert row["status"] == "working"
    assert row["repo"] == "engram"
    assert row["branch"] == "main"
    assert row["repo_remote"] == GITHUB
    assert row["live"] is True
    assert isinstance(row["age_sec"], int) and row["age_sec"] >= 0


def test_office_json_thread_with_last_turn(settings: Settings) -> None:
    _seed_repo(settings)
    brain = settings.brain_path
    _thread_files(brain, "roll-call", "alice", "Who's on deck today?")
    body = _client(_open(settings)).get("/brain/api/office.json").json()
    thread = next(t for t in body["threads"] if t["thread"] == "roll-call")
    assert thread["status"] == "open"
    assert thread["topic"] == "Roll call"
    assert thread["turn_count"] == 1
    assert thread["last_turn"] is not None
    assert thread["last_turn"]["sender"] == "alice"
    assert "Who's on deck today?" in thread["last_turn"]["message"]


def test_office_json_activity_maps_kind_from_paths(settings: Settings) -> None:
    _seed_repo(settings)
    brain = settings.brain_path
    _commit(
        brain,
        "workspace/presence/bob-cc.md",
        _presence_doc("bob-cc", name="Bob-CC", status="idle", updated=_updated(-1)),
        "workspace: presence bob-cc (idle)",
    )
    _thread_files(brain, "standup", "bob", "Standing up the office backend.")
    events = _client(_open(settings)).get("/brain/api/office.json").json()["activity"]
    assert events, "activity feed should not be empty over a real repo"
    kinds = {e["kind"] for e in events}
    assert "presence" in kinds
    assert "thread" in kinds
    # actor + summary derived from the commit subject grammar
    presence_ev = next(e for e in events if e["kind"] == "presence")
    assert presence_ev["session"] == "bob-cc"
    thread_ev = next(e for e in events if e["kind"] == "thread")
    assert thread_ev["session"] == "bob"
    assert "standup" in thread_ev["summary"]


# ------------------------------------------------------------ session dossier


def test_session_dossier_timeline_and_current(settings: Settings) -> None:
    _seed_repo(settings)
    brain = settings.brain_path
    # Two commits to the same presence file → a resume with a status change.
    _commit(
        brain,
        "workspace/presence/alice-cc.md",
        _presence_doc("alice-cc", name="Alice-CC", status="working", updated=_updated(-30)),
        "workspace: presence alice-cc (working)",
    )
    _commit(
        brain,
        "workspace/presence/alice-cc.md",
        _presence_doc("alice-cc", name="Alice-CC", status="blocked", updated=_updated(-2)),
        "workspace: presence alice-cc (blocked)",
    )
    body = _client(_open(settings)).get("/brain/api/session/alice-cc").json()
    assert body["session"] == "alice-cc"
    assert body["current"] is not None
    assert body["current"]["status"] == "blocked"
    assert body["first_seen"]
    assert "engram" in body["repos_touched"]
    kinds = [e["kind"] for e in body["timeline"]]
    assert "appeared" in kinds
    assert "status" in kinds
    # reverse-chron: the newest event is first
    ts = [e["ts"] for e in body["timeline"]]
    assert ts == sorted(ts, reverse=True)


def test_session_dossier_unknown_sid_404(settings: Settings) -> None:
    _seed_repo(settings)
    resp = _client(_open(settings)).get("/brain/api/session/nobody-here")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_session_dossier_traversal_sid_404(settings: Settings) -> None:
    _seed_repo(settings)
    brain = settings.brain_path
    # A real file that a traversal would try to reach — must never be served.
    _commit(
        brain,
        "workspace/presence/alice-cc.md",
        _presence_doc("alice-cc", name="Alice-CC", status="working", updated=_updated(-1)),
        "workspace: presence alice-cc (working)",
    )
    client = _client(_open(settings))
    for evil in ("..", "../alice-cc", "..%2falice-cc", "Alice-CC", "a/b", "a.b"):
        resp = client.get(f"/brain/api/session/{evil}")
        assert resp.status_code == 404, f"{evil!r} should 404, got {resp.status_code}"


# ------------------------------------------------------------ /brain/office page


def test_office_page_serves_html(settings: Settings) -> None:
    resp = _client(_open(settings)).get("/brain/office")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ------------------------------------------------------------ access gate


def test_office_endpoints_stay_guarded(settings: Settings) -> None:
    client = _client(settings)  # dev_no_access False → Access gate holds
    assert client.get("/brain/api/office.json").status_code == 403
    assert client.get("/brain/api/session/alice-cc").status_code == 403
    assert client.get("/brain/office").status_code == 403
