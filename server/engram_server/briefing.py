"""Data-driven morning briefing (no LLM): a rendered snapshot of the brain each day.

Gathers unread inter-session messages, yesterday's log activity, stale artifacts,
and untriaged inbox items, renders a clean markdown briefing, and saves it as a
same-day-idempotent artifact under projects/engram/artifacts/. When there is
anything the user should actually see (unread messages, stale artifacts, inbox
backlog) it also leaves a short pointer message on the engram project.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from anyio import to_thread

from .frontmatter import read_meta, split
from .errors import GitError, KBError

log = logging.getLogger("engram.briefing")

_PROJECT = "engram"


def _gather(store: Any, today: str, yesterday: str) -> dict[str, Any]:
    """Read-only collection over the whole bundle (runs in a worker thread)."""
    projects = store._projects_sync()

    unread: list[dict[str, Any]] = []
    yday_log: list[dict[str, Any]] = []
    for p in projects:
        rel = "metalfinger" if p["id"] == "metalfinger" else f"projects/{p['id']}"
        for msg in store._unread_messages(rel):
            unread.append({**msg, "project": p["id"]})
        log_path = store.root / rel / "log.md"
        if log_path.is_file():
            from .kbstore import _parse_log_entries

            for entry in _parse_log_entries(log_path.read_text(encoding="utf-8")):
                if entry.get("date") == yesterday:
                    yday_log.append({**entry, "project": p["id"]})

    stale = [a for a in store._artifacts_sync(None) if a.get("stale") is True]

    inbox_items: list[dict[str, Any]] = []
    idir = store.root / "inbox"
    if idir.is_dir():
        for f in sorted(idir.glob("*.md")):
            if f.name == "index.md":
                continue
            meta = read_meta(f)
            if str(meta.get("status") or "") == "untriaged":
                inbox_items.append(
                    {
                        "path": f"inbox/{f.name}",
                        "title": str(meta.get("title") or f.stem),
                        "description": str(meta.get("description") or ""),
                    }
                )

    return {
        "projects": projects,
        "unread": unread,
        "yesterday_log": yday_log,
        "stale_artifacts": stale,
        "inbox": inbox_items,
    }


def _render(data: dict[str, Any], today: str) -> tuple[str, list[str]]:
    """Return (markdown_body, source_paths)."""
    sources: list[str] = []
    parts: list[str] = [f"# Morning Briefing — {today}", ""]

    unread = data["unread"]
    parts.append(f"## Unread messages ({len(unread)})")
    parts.append("")
    if unread:
        for m in unread:
            sources.append(m["path"])
            flag = " ⚠️ expired" if m.get("expired") else ""
            parts.append(
                f"* **[{m['project']}]** {m['title']} — {m.get('description', '')} "
                f"(to: {m.get('to', 'any')}, {m.get('priority', 'normal')}){flag}"
            )
    else:
        parts.append("None. ✓")
    parts.append("")

    yday = data["yesterday_log"]
    parts.append(f"## Yesterday's activity ({len(yday)})")
    parts.append("")
    if yday:
        for e in yday:
            parts.append(f"* **[{e['project']}]** {e.get('title', '')}")
    else:
        parts.append("Quiet day. ✓")
    parts.append("")

    stale = data["stale_artifacts"]
    parts.append(f"## Stale artifacts ({len(stale)})")
    parts.append("")
    if stale:
        for a in stale:
            sources.append(a["path"])
            parts.append(f"* {a['title']} — `{a['path']}` (a source changed since build)")
    else:
        parts.append("All current. ✓")
    parts.append("")

    inbox = data["inbox"]
    parts.append(f"## Inbox to triage ({len(inbox)})")
    parts.append("")
    if inbox:
        for it in inbox:
            sources.append(it["path"])
            parts.append(f"* {it['title']} — `{it['path']}`")
    else:
        parts.append("Empty. ✓")
    parts.append("")

    parts.append("## Projects")
    parts.append("")
    for p in data["projects"]:
        parts.append(
            f"* **{p['title']}** ({p['id']}) — last session {p.get('last_session') or '—'}, "
            f"{p.get('unread_messages', 0)} unread"
        )
    parts.append("")

    return "\n".join(parts), sources


def _is_notable(data: dict[str, Any]) -> bool:
    return bool(data["unread"] or data["stale_artifacts"] or data["inbox"])


async def build_briefing(store: Any) -> dict[str, Any]:
    """Build + save today's morning briefing artifact; leave a pointer message when notable.
    Returns a summary dict. Failure-soft on git errors."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    data = await to_thread.run_sync(lambda: _gather(store, today, yesterday))
    body, sources = _render(data, today)

    rel = f"projects/{_PROJECT}/artifacts/{today}-morning-briefing.md"
    if sources:
        sources_yaml = "sources:\n" + "".join(f"  - {s}\n" for s in sources)
    else:
        sources_yaml = "sources: []\n"
    frontmatter = (
        "---\n"
        "type: artifact\n"
        f"title: Morning Briefing {today}\n"
        "description: Daily data-driven briefing across the brain.\n"
        "instruction: daily morning briefing\n"
        f"timestamp: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"{sources_yaml}"
        "---\n"
    )
    content = f"{frontmatter}\n{body}\n"

    result: dict[str, Any] = {"path": rel, "notable": _is_notable(data), "message_left": False}
    try:
        write = await store.kb_write(rel, content, f"briefing: {today}")
        result["sha"] = write.get("sha")
        result["pushed"] = write.get("pushed")
    except (GitError, KBError) as exc:
        log.warning("briefing: write failed: %s", exc)
        result["error"] = str(exc)
        return result

    if _is_notable(data):
        summary_lines = [
            f"{len(data['unread'])} unread, {len(data['stale_artifacts'])} stale artifacts, "
            f"{len(data['inbox'])} inbox items to triage.",
            f"Full briefing: {rel}",
            "Load the engram project to act on messages.",
        ]
        try:
            await store.kb_leave_message(
                _PROJECT,
                f"Morning briefing ready ({today})",
                "\n".join(summary_lines),
                to="any",
            )
            result["message_left"] = True
        except (GitError, KBError) as exc:
            log.warning("briefing: leave_message failed: %s", exc)

    log.info("briefing: %s", {k: result.get(k) for k in ("path", "notable", "message_left")})
    return result
