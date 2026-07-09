"""Server-side auto-presence spool ingest — lock-safe, batched, throttled."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from engram_server.config import Settings
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore, _utcnow
from engram_server.presence_spool import ingest_spool, spool_dir


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


@pytest.fixture
def spool(settings: Settings, tmp_path: Path) -> Path:
    """Point the spool at an isolated dir under tmp and return it (uncreated)."""
    d = tmp_path / "presence-spool"
    settings.presence_spool_dir = str(d)
    return d


def _drop(spool: Path, session: str, **fields) -> Path:
    """Write a spool JSON intent file the way the Claude Code hook would."""
    spool.mkdir(parents=True, exist_ok=True)
    rec = {"session": session, "ts": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    p = spool / f"{session}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def _commit_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def _presence_meta(root: Path, session: str) -> dict:
    return read_meta(root / "workspace" / "presence" / f"{session}.md")


# ------------------------------------------------------------------ ingest writes


async def test_spool_dir_default_when_unset() -> None:
    s = Settings(_env_file=None, presence_spool_dir="")
    assert spool_dir(s) == Path.home() / ".engram" / "presence-spool"


async def test_ingest_writes_presence_and_deletes_spool(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    f = _drop(spool, "pc1-cc", name="CC", repo="engram", branch="main",
              repo_remote="git@github.com:metalfinger/engram.git", cwd="/c/engram",
              status="working", working_on="spool ingest")
    report = await ingest_spool(store, settings)

    assert report == {"ingested": 1, "skipped": 0, "removed": 1}
    assert not f.exists()  # spool file consumed
    meta = _presence_meta(settings.brain_path, "pc1-cc")
    assert meta["session"] == "pc1-cc"
    assert meta["repo"] == "engram"
    assert meta["branch"] == "main"
    assert meta["working_on"] == "spool ingest"
    assert meta["status"] == "working"
    # And it lands in the roster.
    roster = await store.kb_roster()
    assert any(r["session"] == "pc1-cc" for r in roster)


async def test_unchanged_redrop_within_window_dedupes_but_removes(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    _drop(spool, "pc1-cc", repo="engram", branch="main", status="working")
    await ingest_spool(store, settings)
    base = _commit_count(settings.brain_path)

    # Identical re-drop while still fresh — no commit, but the file is still consumed.
    f2 = _drop(spool, "pc1-cc", repo="engram", branch="main", status="working")
    report = await ingest_spool(store, settings)

    assert _commit_count(settings.brain_path) == base  # deduped: no new commit
    assert report["ingested"] == 0
    assert report["skipped"] == 1
    assert report["removed"] == 1
    assert not f2.exists()


async def test_redrop_after_window_refreshes_updated(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    _drop(spool, "pc1-cc", repo="engram", branch="main", status="working")
    await ingest_spool(store, settings)

    # Age the on-disk record past the refresh window so an identical re-drop refreshes it.
    # Commit the edit — the server-owned checkout must stay clean or writes are refused.
    pfile = settings.brain_path / "workspace" / "presence" / "pc1-cc.md"
    settings.presence_refresh_minutes = 5
    old = "2000-01-01T00:00:00Z"
    text = pfile.read_text(encoding="utf-8")
    aged = re.sub(r"(?m)^updated:.*$", f"updated: '{old}'", text)
    assert aged != text  # the aging actually landed
    pfile.write_text(aged, encoding="utf-8", newline="\n")
    root = settings.brain_path
    store.repo.commit_and_push(["workspace/presence/pc1-cc.md"], "age presence")
    base = _commit_count(root)

    _drop(spool, "pc1-cc", repo="engram", branch="main", status="working")
    report = await ingest_spool(store, settings)

    assert report["ingested"] == 1  # stale -> refreshed even though content is identical
    assert _commit_count(settings.brain_path) == base + 1
    assert _presence_meta(settings.brain_path, "pc1-cc")["updated"] != old


async def test_status_done_sets_presence_done(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    _drop(spool, "pc1-cc", repo="engram", status="working")
    await ingest_spool(store, settings)
    _drop(spool, "pc1-cc", repo="engram", status="done")
    await ingest_spool(store, settings)

    assert _presence_meta(settings.brain_path, "pc1-cc")["status"] == "done"
    # The presence file is kept (housekeeping prunes it later), not deleted.
    assert (settings.brain_path / "workspace" / "presence" / "pc1-cc.md").exists()


async def test_status_ended_maps_to_done(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    _drop(spool, "pc1-cc", status="ended")
    await ingest_spool(store, settings)
    assert _presence_meta(settings.brain_path, "pc1-cc")["status"] == "done"


async def test_malformed_json_skipped_and_deleted(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    spool.mkdir(parents=True, exist_ok=True)
    bad = spool / "broken.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    # A JSON object with no session is also unusable.
    nosess = spool / "nosess.json"
    nosess.write_text(json.dumps({"repo": "x"}), encoding="utf-8")

    report = await ingest_spool(store, settings)

    assert report["ingested"] == 0
    assert report["skipped"] == 2
    assert report["removed"] == 2
    assert not bad.exists()
    assert not nosess.exists()


async def test_batch_of_many_is_one_commit(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    base = _commit_count(settings.brain_path)
    for i in range(4):
        _drop(spool, f"pc{i}-cc", repo="engram", branch="main", status="working")

    report = await ingest_spool(store, settings)

    assert report["ingested"] == 4
    assert _commit_count(settings.brain_path) == base + 1  # ONE commit for the whole batch
    for i in range(4):
        assert _presence_meta(settings.brain_path, f"pc{i}-cc")["repo"] == "engram"


async def test_missing_spool_dir_is_clean_noop(
    store: KBStore, settings: Settings, spool: Path
) -> None:
    assert not spool.exists()
    base = _commit_count(settings.brain_path)
    report = await ingest_spool(store, settings)
    assert report == {"ingested": 0, "skipped": 0, "removed": 0}
    assert _commit_count(settings.brain_path) == base
