"""Server-side ingest of the auto-presence spool.

Claude Code hooks (SessionStart / UserPromptSubmit / SessionEnd) drop tiny plain-JSON
intent files into the spool dir — a bare file write, never git. This module reads those
files and upserts each into the brain's presence records THROUGH the server's single
write lock (one batched commit per tick), then deletes the spool files. Nothing here
writes the checkout outside ``_locked_commit``, so there is never a second git writer
racing the server. Fully failure-soft: a bad file is dropped, a missing dir is a no-op,
and any error is logged and swallowed so the scheduler tick never crashes.

Spool file: ``<spool_dir>/<session>.json`` — a JSON object with:
    session      str   (required) session id/handle; slugged to the presence key
    name         str?  human label for the session
    cwd          str?  working directory
    repo         str?  repo name
    branch       str?  git branch
    repo_remote  str?  git remote URL
    host         str?  hostname / PC
    project      str?  project id
    status       str?  working | idle | blocked | available | done (ended -> done)
    working_on   str?  one-line what-I'm-doing
    ts           str?  ISO-8601 timestamp the hook fired (informational; the server
                       stamps its own `updated` on write)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from anyio import to_thread

from .config import Settings

log = logging.getLogger("engram.presence_spool")


def spool_dir(settings: Settings) -> Path:
    """Resolve the spool directory: ``settings.presence_spool_dir`` when set, else the
    default ``~/.engram/presence-spool``."""
    raw = (settings.presence_spool_dir or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".engram" / "presence-spool"


def _read_spool(dir_path: Path) -> list[tuple[Path, dict[str, Any] | None]]:
    """List and parse every ``*.json`` in the spool dir (sync; runs in a worker thread).

    Returns ``(path, record)`` pairs; ``record`` is ``None`` for a file that will not
    parse into an object with a non-empty ``session``. A missing dir yields ``[]``."""
    out: list[tuple[Path, dict[str, Any] | None]] = []
    if not dir_path.is_dir():
        return out
    for f in sorted(dir_path.glob("*.json")):
        rec: dict[str, Any] | None = None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("session") or "").strip():
                rec = data
        except (OSError, ValueError):
            rec = None
        out.append((f, rec))
    return out


def _unlink(path: Path) -> None:
    """Delete a consumed spool file; a missing/locked file is not an error."""
    try:
        path.unlink()
    except OSError:
        pass


async def ingest_spool(store: Any, settings: Settings) -> dict[str, int]:
    """Ingest one tick of the presence spool.

    Reads every spool file, upserts the valid ones into presence via the store's batched
    write path (ONE commit through ``_locked_commit``), then deletes the spool files it
    consumed. Returns ``{ingested, skipped, removed}`` — ``ingested`` records caused a
    presence write, ``skipped`` were malformed or deduped no-ops, ``removed`` spool files
    were deleted. Failure-soft: any error is logged and swallowed."""
    report = {"ingested": 0, "skipped": 0, "removed": 0}
    try:
        dir_path = spool_dir(settings)
        entries = await to_thread.run_sync(_read_spool, dir_path)
        if not entries:
            return report

        records: list[dict[str, Any]] = []
        good_files: list[Path] = []
        for path, rec in entries:
            if rec is None:
                # Malformed / unparseable — drop it so it never wedges the tick.
                await to_thread.run_sync(_unlink, path)
                report["skipped"] += 1
                report["removed"] += 1
                continue
            records.append(rec)
            good_files.append(path)

        if records:
            written: list[str] = []
            await store._locked_commit(
                lambda: store._presence_write_batch(
                    records, settings.presence_refresh_minutes, written
                ),
                f"workspace: presence ingest ({len(records)} spooled)",
            )
            report["ingested"] = len(written)
            report["skipped"] += len(records) - len(written)
            # Every parsed spool file is consumed — whether it triggered a write or
            # was a deduped no-op — so a heartbeat re-drop never piles up.
            for path in good_files:
                await to_thread.run_sync(_unlink, path)
                report["removed"] += 1

        if report["ingested"] or report["skipped"]:
            log.info(
                "presence spool: ingested=%d skipped=%d removed=%d",
                report["ingested"],
                report["skipped"],
                report["removed"],
            )
    except Exception:  # noqa: BLE001 — ingest must never crash the scheduler tick
        log.warning("presence spool: ingest failed", exc_info=True)
    return report
