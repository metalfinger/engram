"""Living-office JSON backend: pure, read-only derivations for /brain/office.

Three sync builders (the route runs each in a worker thread — asyncio subprocesses
break under the Windows selector loop, mirroring the existing ``_git_log`` pattern):

* ``office_payload``   -> the /brain/api/office.json body (sessions + threads + activity)
* ``activity_events``  -> git-log-derived commit events, newest-first, kind by path
* ``session_dossier``  -> the /brain/api/session/{sid}.json body (cross-resume timeline)

Everything here is a pure read of the checkout + ``git log`` / ``git show``; NOTHING
writes. The roster/thread helpers live in ``routes`` and are imported lazily inside the
functions so this module and ``routes`` can import each other without a load-time cycle.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

from engram_server.explorer.format import _to_utc
from engram_server.explorer.render import split_frontmatter

# A presence session id is the kbstore ``_slug`` output: lowercase alnum + hyphens,
# leading alnum. The leading-alnum anchor rejects "-x"; the class excludes ".", "/",
# and uppercase, so ".", "..", ".git", and any traversal segment can never match — a
# validated sid is safe to hand to git as a pathspec (``-- workspace/presence/<sid>.md``).
_SAFE_SID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Read-only git budget (seconds). office.json is polled ~2.5s; keep it snappy.
_GIT_TIMEOUT = 10.0

# How many presence-file revisions we walk for a dossier timeline. Presence commits are
# throttled (>5 min stale OR a meaningful field change), so this spans many resumes.
_DOSSIER_REVS = 40

_LIVE_WINDOW_SEC = 15 * 60  # a session is "live" when its heartbeat is <= 15 min old

# Kind priority — the most meaningful path in a commit wins (a handoff that also drops a
# pointer turn into a room reads as "handoff", not "thread"). Lower rank = higher priority.
_KIND_RANK = {
    "handoff": 0,
    "decision": 1,
    "artifact": 2,
    "thread": 3,
    "presence": 4,
    "log": 5,
    "concept": 6,
    "other": 7,
}


# ---------------------------------------------------------------- slug / time helpers


def _slugify(value: str) -> str:
    """kbstore ``_slug`` semantics: lowercase, each non-alnum run -> one hyphen, trimmed.

    Used to match a thread turn's ``sender`` (a free display handle) against a presence
    session id so a session's own turns land on its dossier timeline.
    """
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _iso_z(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_sec(d: dt.datetime | None, now: dt.datetime) -> int | None:
    if d is None:
        return None
    return max(0, int((now - d).total_seconds()))


# ---------------------------------------------------------------- git (read-only)


def _git(brain: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only git command in the checkout; None on any failure (never raises)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(brain),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def _git_log_named(brain: Path, limit: int) -> list[tuple[str, str, str, str, list[str]]]:
    """Last ``limit`` commits as (sha, iso-date, author, subject, changed_paths).

    Uses ``--name-only`` with a record-separator (\\x1e) prefixing every commit and a
    field-separator (\\x1f) between metadata fields, so the changed-path list (which
    follows on its own lines) is unambiguous even when a subject contains odd bytes.
    """
    proc = _git(
        brain,
        [
            "log",
            "-n",
            str(limit),
            "--date=iso-strict",
            "--name-only",
            "--pretty=format:%x1e%h%x1f%ad%x1f%an%x1f%s",
        ],
    )
    if proc is None or proc.returncode != 0:
        return []
    rows: list[tuple[str, str, str, str, list[str]]] = []
    for chunk in proc.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        fields = lines[0].split("\x1f")
        if len(fields) < 4:
            continue
        sha, date, author, subject = fields[0], fields[1], fields[2], fields[3]
        paths = [ln.strip() for ln in lines[1:] if ln.strip()]
        rows.append((sha, date, author, subject, paths))
    return rows


def _git_log_file(brain: Path, path: str, limit: int) -> list[tuple[str, str]]:
    """(sha, iso-date) for the commits that touched ``path``, newest-first.

    ``path`` is passed after ``--`` as a literal pathspec — never interpolated into a
    shell — and the caller has already validated the sid, so no traversal is possible.
    """
    proc = _git(
        brain,
        ["log", "-n", str(limit), "--date=iso-strict", "--pretty=format:%H%x1f%ad", "--", path],
    )
    if proc is None or proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if "\x1f" in line:
            sha, date = line.split("\x1f", 1)
            out.append((sha, date))
    return out


def _git_show(brain: Path, sha: str, path: str) -> str | None:
    """The text of ``path`` at revision ``sha`` (``git show sha:path``); None if absent."""
    proc = _git(brain, ["show", f"{sha}:{path}"])
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


# ---------------------------------------------------------------- activity kind + actor


def _kind_for_path(p: str) -> str:
    """Classify one changed path into the contract's activity-kind vocabulary."""
    if p.startswith("workspace/handoffs/"):
        return "handoff"
    if p.startswith("threads/"):
        return "thread"
    if p.startswith("workspace/presence/") or p.startswith("workspace/claims/"):
        return "presence" if "presence" in p else "other"
    if p.startswith("projects/") and "/decisions/" in p:
        return "decision"
    if "/artifacts/" in p:
        return "artifact"
    if p == "log.md" or p.endswith("/log.md"):
        return "log"
    if p.endswith(".md"):
        return "concept"
    return "other"


def _commit_kind(paths: list[str]) -> str:
    """The single kind for a commit: the highest-priority kind among its paths.

    index.md paths are ignored when any content path exists (an index bump is a side
    effect of the real write, never the story), but they still classify a lone-index
    commit rather than defaulting it to 'other'.
    """
    content = [p for p in paths if not p.endswith("index.md")] or paths
    if not content:
        return "other"
    kinds = {_kind_for_path(p) for p in content}
    return min(kinds, key=lambda k: _KIND_RANK.get(k, 99))


def _actor_and_summary(subject: str) -> tuple[str, str]:
    """Best-effort (session_actor, human_summary) from a commit subject.

    Maps the server's own commit-message grammar (see kbstore ``_locked_commit`` calls):
    ``thread: <tid> <sender> <verb>``, ``workspace: presence <sid> (<st>)``,
    ``workspace: presence ingest (N spooled)``, ``workspace: handoff from <sid>``,
    ``workspace: claim|release <slug>``. Anything else -> ("", the subject verbatim).
    """
    s = subject.strip()
    if s.startswith("thread: "):
        toks = s[len("thread: ") :].split()
        tid = toks[0] if toks else ""
        sender = toks[1] if len(toks) > 1 else ""
        verb = toks[2] if len(toks) > 2 else "posted"
        return sender, (f"{verb} → {tid}" if tid else s)
    if s.startswith("workspace: presence ingest"):
        return "", s[len("workspace: ") :]  # "presence ingest (N spooled)"
    if s.startswith("workspace: presence "):
        rest = s[len("workspace: presence ") :].strip()
        sid = rest.split(" (", 1)[0].strip()
        st = ""
        if "(" in rest and ")" in rest:
            st = rest[rest.find("(") + 1 : rest.find(")")].strip()
        return sid, (f"presence → {st}" if st else "presence update")
    if s.startswith("workspace: handoff from "):
        frm = s[len("workspace: handoff from ") :].strip()
        return frm, f"handoff from {frm}"
    if s.startswith("workspace: claim "):
        slug = s[len("workspace: claim ") :].strip()
        return slug, f"claimed {slug}"
    if s.startswith("workspace: release "):
        slug = s[len("workspace: release ") :].strip()
        return slug, f"released {slug}"
    return "", s


def activity_events(brain: Path, limit: int = 40) -> list[dict]:
    """Git-log-derived activity, newest-first: {ts, age_sec, kind, session, summary, paths}.

    ``kind`` is derived primarily from the changed paths (robust); the commit subject is
    a secondary signal only for the best-effort ``session`` actor and a human summary.
    """
    now = dt.datetime.now(dt.timezone.utc)
    out: list[dict] = []
    for _sha, date, _author, subject, paths in _git_log_named(brain, limit):
        d = _to_utc(date)
        session, summary = _actor_and_summary(subject)
        out.append(
            {
                "ts": _iso_z(d) if d else date,
                "age_sec": _age_sec(d, now),
                "kind": _commit_kind(paths),
                "session": session,
                "summary": summary,
                "paths": paths[:12],
            }
        )
    return out


# ---------------------------------------------------------------- rooms

# The heartbeat gap (seconds) above which a session's history is read as a RETURN — it
# was away long enough that reappearing is a "welcome back", not one continuous run.
# Presence re-commits at most every ~5 min (staleness) or on a real change, so a gap this
# size means the session genuinely went dark and came back.
_RETURN_GAP_SEC = 20 * 60

_UNASSIGNED_LABEL = "Unassigned / visiting"


def _known_projects(brain: Path) -> dict[str, str]:
    """Map of project id -> display title for every ``projects/<id>/`` dir.

    Title is the project's context.md frontmatter ``title`` (falling back to the id).
    Used only to give a session's room a human label and a deterministic layout — a room
    is a VISUAL home, never an access boundary (any session reads any project via kb_*).
    """
    out: dict[str, str] = {}
    proot = brain / "projects"
    if not proot.is_dir():
        return out
    for pdir in sorted(proot.iterdir()):
        if not pdir.is_dir() or pdir.name.startswith("."):
            continue
        title = pdir.name
        ctx = pdir / "context.md"
        if ctx.is_file():
            try:
                meta, _body = split_frontmatter(ctx.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                meta = {}
            if isinstance(meta, dict) and meta.get("title"):
                title = str(meta["title"])
        out[pdir.name] = title
    return out


def _room_for(meta: dict, projects: dict[str, str]) -> tuple[str, str, str]:
    """Resolve (room_id, room_label, kind) for a presence record.

    Order: a non-empty presence ``project`` wins (its own room); else a ``repo`` that
    matches a known projects/ dir folds into that project room; else the ``repo`` is its
    own room; else ``unassigned``. kind ∈ project|repo|unassigned.
    """
    project = str(meta.get("project") or "").strip()
    repo = str(meta.get("repo") or "").strip()
    if project:
        return project, projects.get(project, project), "project"
    if repo and repo in projects:
        return repo, projects[repo], "project"
    if repo:
        return repo, repo, "repo"
    return "unassigned", _UNASSIGNED_LABEL, "unassigned"


def _rooms_from(rows: list[dict]) -> list[dict]:
    """The distinct rooms present across the session rows, project-first then repo.

    Derived purely from the sessions so the frontend can lay out rooms even before any
    character arrives; ordering is deterministic (kind, then label).
    """
    seen: dict[str, dict] = {}
    for r in rows:
        rid = r["room"]
        if rid not in seen:
            seen[rid] = {"id": rid, "label": r["room_label"], "kind": r["room_kind"]}
    rank = {"project": 0, "repo": 1, "unassigned": 2}
    return sorted(seen.values(), key=lambda x: (rank.get(x["kind"], 9), x["label"].lower()))


# ---------------------------------------------------------------- sessions


def _first_seen_returning(brain: Path, sid: str, now: dt.datetime) -> tuple[str, bool]:
    """(first_seen_iso, returning) from the presence file's commit history.

    ``first_seen`` is the oldest presence commit's date; ``returning`` is true when any
    consecutive gap in that history exceeds ``_RETURN_GAP_SEC`` (the session went dark and
    came back). Empty/False when there is no git history — the caller falls back to
    ``updated`` for first_seen.
    """
    if not sid or not _SAFE_SID_RE.match(sid):
        return "", False
    commits = _git_log_file(brain, f"workspace/presence/{sid}.md", 100)
    dates = sorted(d for d in (_to_utc(c[1]) for c in commits) if d is not None)
    if not dates:
        return "", False
    returning = any(
        (dates[i + 1] - dates[i]).total_seconds() > _RETURN_GAP_SEC for i in range(len(dates) - 1)
    )
    return _iso_z(dates[0]), returning


def _session_row(brain: Path, meta: dict, now: dt.datetime, projects: dict[str, str]) -> dict:
    """A presence frontmatter dict -> the sessions[] row shape.

    Adds age_sec + live, the returning-employee flags (first_seen + returning), and the
    resolved visual room (room / room_label / room_kind).
    """
    d = _to_utc(meta.get("updated"))
    age = _age_sec(d, now)
    # Normalize to canonical ISO-Z: the YAML loader may hand back a datetime (it
    # auto-parses timestamp scalars), so str() alone would emit a space-separated form.
    updated = _iso_z(d) if d else str(meta.get("updated") or "")
    sid = str(meta.get("session") or "")
    room, room_label, room_kind = _room_for(meta, projects)
    first_seen, returning = _first_seen_returning(brain, sid, now)
    if not first_seen:
        first_seen = updated  # fall back to the current heartbeat when there's no history
    return {
        "session": sid,
        "name": str(meta.get("name") or ""),
        "status": str(meta.get("status") or "unknown"),
        "working_on": str(meta.get("working_on") or ""),
        "repo": str(meta.get("repo") or ""),
        "branch": str(meta.get("branch") or ""),
        "repo_remote": str(meta.get("repo_remote") or ""),
        "project": str(meta.get("project") or ""),
        "host": str(meta.get("host") or ""),
        "updated": updated,
        "age_sec": age,
        "live": age is not None and age <= _LIVE_WINDOW_SEC,
        "first_seen": first_seen,
        "returning": returning,
        "room": room,
        "room_label": room_label,
        "room_kind": room_kind,
    }


def _sessions(brain: Path, now: dt.datetime, projects: dict[str, str]) -> list[dict]:
    """All presence records as sessions[] rows, live-first then newest-heartbeat first."""
    from engram_server.explorer import routes as _routes  # lazy: avoid import cycle

    rows = [_session_row(brain, m, now, projects) for m in _routes._presence_records(brain)]
    big = 10**12
    rows.sort(key=lambda r: (not r["live"], r["age_sec"] if r["age_sec"] is not None else big))
    return rows


# ---------------------------------------------------------------- threads


def _threads(brain: Path, now: dt.datetime) -> list[dict]:
    """Thread summaries in the office shape, adding ``last_turn`` (newest turn) + turn_count."""
    from engram_server.explorer import routes as _routes  # lazy: avoid import cycle

    troot = brain / "threads"
    out: list[dict] = []
    for summary in _routes._thread_summaries(brain):
        tid = summary["id"]
        turns = _routes._thread_turns(brain / "threads" / tid)
        last_turn = None
        if turns:
            meta, body = turns[-1]
            last_turn = {
                "sender": str(meta.get("sender") or "unknown"),
                "message": body.strip(),
                "timestamp": str(meta.get("timestamp") or ""),
            }
        out.append(
            {
                "thread": tid,
                "status": summary["status"],
                "topic": summary["topic"],
                "participants": summary["participants"],
                "turn_count": summary["turns"],
                "last_activity": summary["last_activity"],
                "last_turn": last_turn,
            }
        )
    return out


# ---------------------------------------------------------------- office payload


def office_payload(brain: Path, now: dt.datetime) -> dict:
    """The /brain/api/office.json body: now + sessions + recent + rooms + threads + activity.

    ``sessions`` holds ONLY live rows (heartbeat within the 15-min window) — these are the
    characters the office renders. Stale rows move to ``recent`` (same row shape) so the UI
    can list who was recently around WITHOUT spawning a ghost character for a dead session.
    ``rooms`` is derived from the LIVE sessions only, so an office with only stale sessions
    stays empty rather than showing a populated-looking room.
    """
    projects = _known_projects(brain)
    all_rows = _sessions(brain, now, projects)
    live = [r for r in all_rows if r["live"]]
    recent = [r for r in all_rows if not r["live"]]
    return {
        "now": _iso_z(now),
        "sessions": live,
        "recent": recent,
        "rooms": _rooms_from(live),
        "threads": _threads(brain, now),
        "activity": activity_events(brain, 40),
    }


# ---------------------------------------------------------------- session dossier


def _presence_at(brain: Path, sha: str, path: str) -> dict | None:
    """Frontmatter of the presence file at revision ``sha`` (None if unreadable)."""
    text = _git_show(brain, sha, path)
    if text is None:
        return None
    try:
        meta, _body = split_frontmatter(text)
    except (ValueError, OSError):
        return None
    return meta if isinstance(meta, dict) else None


def _presence_timeline(brain: Path, sid: str, path: str, now: dt.datetime) -> tuple[list[dict], set[str], set[str], str]:
    """Walk the presence file's history oldest->newest, emitting change events.

    Returns (events, repos_touched, projects_touched, first_seen). The oldest revision
    yields an 'appeared' event; each later revision whose status / repo / project differs
    from the running value yields a 'status' / 'repo' / 'project' event. Distinct repos
    and projects seen across all revisions are accumulated for the dossier header.
    """
    revs = list(reversed(_git_log_file(brain, path, _DOSSIER_REVS)))  # oldest-first
    events: list[dict] = []
    repos: set[str] = set()
    projects: set[str] = set()
    first_seen = ""
    prev_status = prev_repo = prev_project = None
    for i, (sha, date) in enumerate(revs):
        meta = _presence_at(brain, sha, path)
        if meta is None:
            continue
        d = _to_utc(date)
        ts = _iso_z(d) if d else date
        age = _age_sec(d, now)
        status = str(meta.get("status") or "")
        repo = str(meta.get("repo") or "")
        project = str(meta.get("project") or "")
        host = str(meta.get("host") or "")
        if repo:
            repos.add(repo)
        if project:
            projects.add(project)
        if not first_seen:
            first_seen = ts
        if i == 0 or prev_status is None:
            events.append(
                {"ts": ts, "age_sec": age, "kind": "appeared",
                 "summary": f"first seen on {host}" if host else "first seen"}
            )
        else:
            if repo != prev_repo and repo:
                events.append({"ts": ts, "age_sec": age, "kind": "repo",
                               "summary": f"moved to {repo}" + (f"@{meta.get('branch')}" if meta.get("branch") else "")})
            if project != prev_project and project:
                events.append({"ts": ts, "age_sec": age, "kind": "project",
                               "summary": f"project → {project}"})
            if status != prev_status and status:
                rb = f" · {repo}" + (f"@{meta.get('branch')}" if meta.get("branch") else "") if repo else ""
                events.append({"ts": ts, "age_sec": age, "kind": "status",
                               "summary": f"status → {status}{rb}"})
        prev_status, prev_repo, prev_project = status, repo, project
    return events, repos, projects, first_seen


def _thread_events(brain: Path, sid: str, now: dt.datetime) -> tuple[list[dict], list[str]]:
    """Timeline events for the session's own thread turns + the list of rooms it joined.

    A turn belongs to this session when its ``sender`` slugs to ``sid``; a room is
    'joined' when the session sent a turn there or is a slugged participant.
    """
    from engram_server.explorer import routes as _routes  # lazy: avoid import cycle

    events: list[dict] = []
    joined: list[str] = []
    for summary in _routes._thread_summaries(brain):
        tid = summary["id"]
        mine = _slugify(sid) in {_slugify(p) for p in summary["participants"]}
        turns = _routes._thread_turns(brain / "threads" / tid)
        for meta, _body in turns:
            if _slugify(str(meta.get("sender") or "")) != sid:
                continue
            mine = True
            d = _to_utc(meta.get("timestamp"))
            events.append(
                {
                    "ts": _iso_z(d) if d else str(meta.get("timestamp") or ""),
                    "age_sec": _age_sec(d, now),
                    "kind": "thread",
                    "summary": f"posted in {tid}",
                }
            )
        if mine:
            joined.append(tid)
    return events, joined


def session_dossier(brain: Path, sid: str, now: dt.datetime) -> dict | None:
    """The /brain/api/session/{sid}.json body, or None (-> 404) for an invalid/unknown sid.

    ``sid`` MUST match the safe-slug regex (traversal-proof); a sid with neither a live
    presence file nor any git history returns None.
    """
    if not _SAFE_SID_RE.match(sid):
        return None
    path = f"workspace/presence/{sid}.md"
    pfile = brain / "workspace" / "presence" / f"{sid}.md"
    history = _git_log_file(brain, path, _DOSSIER_REVS)
    if not pfile.is_file() and not history:
        return None

    p_events, repos, projects, first_seen = _presence_timeline(brain, sid, path, now)
    t_events, joined = _thread_events(brain, sid, now)

    # 'wrote' events: commits attributable to this session (handoff/claim/release/etc),
    # excluding the presence + thread streams already covered above.
    w_events: list[dict] = []
    for ev in activity_events(brain, 40):
        if ev["session"] == sid and ev["kind"] not in ("presence", "thread"):
            w_events.append(
                {"ts": ev["ts"], "age_sec": ev["age_sec"], "kind": "wrote", "summary": ev["summary"]}
            )

    timeline = p_events + t_events + w_events
    timeline.sort(key=lambda e: e["ts"], reverse=True)

    current = None
    name = ""
    if pfile.is_file():
        try:
            meta, _body = split_frontmatter(pfile.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError):
            meta = {}
        if isinstance(meta, dict) and meta:
            current = _session_row(brain, meta, now, _known_projects(brain))
            name = current["name"]
            if current["repo"]:
                repos.add(current["repo"])
            if current["project"]:
                projects.add(current["project"])

    return {
        "session": sid,
        "name": name,
        "current": current,
        "first_seen": first_seen,
        "projects_touched": sorted(projects),
        "repos_touched": sorted(repos),
        "threads_joined": sorted(set(joined)),
        "timeline": timeline[:60],
    }
