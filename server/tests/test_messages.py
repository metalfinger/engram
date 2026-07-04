"""kb_leave_message / kb_mark_read: lifecycle, index regeneration, expiry."""

from __future__ import annotations

import posixpath
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _remote_head(remote_repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(remote_repo), "rev-parse", "main"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


async def test_leave_message_surfaces_everywhere(
    store: KBStore, settings: Settings, remote_repo: Path
) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    res = await store.kb_leave_message(
        "materia-verde",
        "Verify DNS before CMS work",
        "Check dig for materiaverde.com before publishing. If still on Wix nameservers, stop.",
        to="claude-code",
        priority="high",
        expires="2099-01-01",
    )
    assert res["path"] == f"projects/materia-verde/messages/{today}-verify-dns-before-cms-work.md"
    assert res["warnings"] == []
    assert res["pushed"] is True
    assert _remote_head(remote_repo) == res["sha"]

    # kb_load surfaces the full body
    load = await store.kb_load("materia-verde")
    assert len(load["unread_messages"]) == 1
    msg = load["unread_messages"][0]
    assert msg["path"] == res["path"]
    assert msg["title"] == "Verify DNS before CMS work"
    assert msg["description"] == "Check dig for materiaverde.com before publishing."
    assert msg["to"] == "claude-code"
    assert msg["priority"] == "high"
    assert msg["expires"] == "2099-01-01"
    assert msg["expired"] is False
    assert "If still on Wix nameservers, stop." in msg["body"]

    # kb_projects counts it
    rows = await store.kb_projects()
    counts = {r["id"]: r["unread_messages"] for r in rows}
    assert counts["materia-verde"] == 1
    assert counts["alt"] == 0

    # messages index regenerated canonically
    idx = (settings.brain_path / "projects/materia-verde/messages/index.md").read_text(
        encoding="utf-8"
    )
    assert "# Messages" in idx
    assert (
        f"* [Verify DNS before CMS work]({today}-verify-dns-before-cms-work.md) - "
        "Check dig for materiaverde.com before publishing. (to: claude-code, high)" in idx
    )
    assert "No unread messages." not in idx
    assert idx.rstrip().endswith("Read messages live in [archive/](archive/index.md).")


async def test_leave_message_creates_dirs_lazily_and_warns_on_unknown_to(
    store: KBStore, settings: Settings
) -> None:
    assert not (settings.brain_path / "metalfinger/messages").exists()
    res = await store.kb_leave_message(
        "metalfinger", "Test note", "A note.", to="carrier-pigeon"
    )
    assert res["path"].startswith("metalfinger/messages/")
    assert len(res["warnings"]) == 1
    assert "carrier-pigeon" in res["warnings"][0]
    assert (settings.brain_path / "metalfinger/messages/index.md").is_file()
    assert (settings.brain_path / "metalfinger/messages/archive/index.md").is_file()
    load = await store.kb_load("metalfinger")
    assert [m["path"] for m in load["unread_messages"]] == [res["path"]]


async def test_leave_message_filename_collision_gets_suffix(store: KBStore) -> None:
    r1 = await store.kb_leave_message("alt", "Same title", "One.")
    r2 = await store.kb_leave_message("alt", "Same title", "Two.")
    assert r2["path"] == r1["path"].replace(".md", "-2.md")


async def test_leave_message_rejects_bad_expires(store: KBStore) -> None:
    with pytest.raises(KBError, match="YYYY-MM-DD"):
        await store.kb_leave_message("alt", "Soon", "Body.", expires="soon")
    with pytest.raises(KBError, match="YYYY-MM-DD"):
        await store.kb_leave_message("alt", "Bad date", "Body.", expires="2026-13-45")


async def test_expired_yesterday_is_flagged(store: KBStore) -> None:
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    res = await store.kb_leave_message("alt", "Old news", "This has expired.", expires=yesterday)
    load = await store.kb_load("alt")
    msg = next(m for m in load["unread_messages"] if m["path"] == res["path"])
    assert msg["expires"] == yesterday
    assert msg["expired"] is True


async def test_mark_read_archives_and_cleans_index(
    store: KBStore, settings: Settings, remote_repo: Path
) -> None:
    res = await store.kb_leave_message(
        "alt", "Do the thing", "Please do the thing. Then stop.", to="claude-code"
    )
    name = posixpath.basename(res["path"])

    r = await store.kb_mark_read(res["path"])
    assert r["archived_path"] == f"projects/alt/messages/archive/{name}"
    assert r["pushed"] is True
    assert not (settings.brain_path / res["path"]).exists()

    archived = (settings.brain_path / r["archived_path"]).read_text(encoding="utf-8")
    assert "status: read" in archived
    assert "read_at:" in archived
    assert "Please do the thing." in archived

    idx = (settings.brain_path / "projects/alt/messages/index.md").read_text(encoding="utf-8")
    assert "No unread messages." in idx
    assert name not in idx

    load = await store.kb_load("alt")
    assert load["unread_messages"] == []
    rows = await store.kb_projects()
    assert next(row for row in rows if row["id"] == "alt")["unread_messages"] == 0

    assert _remote_head(remote_repo) == r["sha"]
    assert store.repo.run("log", "-1", "--format=%s").strip() == f"msg: read {name}"


async def test_mark_read_rejections(store: KBStore) -> None:
    with pytest.raises(KBError, match="not a message"):
        await store.kb_mark_read("projects/alt/messages/index.md")
    with pytest.raises(KBError, match="already archived"):
        await store.kb_mark_read("projects/alt/messages/archive/2026-01-01-old.md")
    with pytest.raises(KBError, match="messages/"):
        await store.kb_mark_read("projects/alt/context.md")
    with pytest.raises(KBError, match="No such message"):
        await store.kb_mark_read("projects/alt/messages/2099-01-01-nope.md")


async def test_mark_read_rejects_non_message_type(store: KBStore, other_clone) -> None:
    # A stray non-message file dropped into messages/ from another PC.
    other_clone.commit_file(
        "projects/alt/messages/2026-07-04-fake.md",
        "---\ntype: note\ntitle: Fake\n---\nNot a message.\n",
        "sneak a note in",
    )
    with pytest.raises(KBError, match="type: message"):
        await store.kb_mark_read("projects/alt/messages/2026-07-04-fake.md")
