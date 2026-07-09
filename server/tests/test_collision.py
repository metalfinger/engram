"""Collision safety — optimistic-concurrency base_hash guard, advisory kb_claim/release/
claims, thread/handoff secret-scan, and the seq/notified/refs/id-validation hardening."""

from __future__ import annotations

import subprocess
from datetime import timedelta

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.frontmatter import read_meta
from engram_server.kbstore import _CLAIM_TTL_MIN, KBStore, _slug, _utcnow


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _concept(body: str = "Body text.") -> str:
    """A minimal valid OKF concept that links to the project context (no link-less nag)."""
    return f"---\ntype: note\ndescription: A note.\n---\n\n{body}\n\n- [context](../context.md)\n"


def _plant_claim(root, path: str, session: str, claimed_at: str) -> None:
    """Drop a claim record straight onto the checkout so its claimed_at controls TTL/age."""
    slug = _slug(path, "claim")
    p = root / "workspace" / "claims" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = [
        "type: claim",
        f"title: 'Claim: {path}'",
        f"description: {path}",
        f"path: {path}",
        f"session: {session}",
        "note: ''",
        f"claimed_at: {claimed_at}",
    ]
    p.write_text("---\n" + "\n".join(meta) + "\n---\n", encoding="utf-8", newline="\n")


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit_all(root, msg: str = "test: plant") -> None:
    """Commit planted/corrupted files so the server-owned checkout stays clean (the
    clean-checkout guard blocks a mutation otherwise). Uses the suite's isolated git env."""
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", msg], check=True, capture_output=True)


# ------------------------------------------------------------------ base_hash concurrency guard


async def test_kb_read_returns_content_hash(store: KBStore) -> None:
    path = "projects/alt/notes/hashed.md"
    await store.kb_write(path, _concept(), "add hashed")
    rd = await store.kb_read(path)
    assert isinstance(rd.get("hash"), str) and len(rd["hash"]) == 64
    # The hash is over the returned content, byte-for-byte.
    import hashlib

    assert rd["hash"] == hashlib.sha256(rd["content"].encode("utf-8")).hexdigest()


async def test_base_hash_match_allows_write(store: KBStore) -> None:
    path = "projects/alt/notes/match.md"
    await store.kb_write(path, _concept("One."), "create")
    rd = await store.kb_read(path)
    res = await store.kb_write(path, _concept("Two."), "update", base_hash=rd["hash"])
    assert res["no_change"] is False
    assert "Two." in (store.root / path).read_text(encoding="utf-8")


async def test_base_hash_mismatch_rejects_write(store: KBStore) -> None:
    path = "projects/alt/notes/conflict.md"
    await store.kb_write(path, _concept("Original."), "create")
    stale = (await store.kb_read(path))["hash"]
    # Another session edits the file in between — the hash we hold is now stale.
    await store.kb_edit(path, "append", "Concurrent edit.")
    with pytest.raises(KBError, match="changed since you read it"):
        await store.kb_write(path, _concept("Mine."), "update", base_hash=stale)
    # Nothing was overwritten: the concurrent edit survives.
    assert "Concurrent edit." in (store.root / path).read_text(encoding="utf-8")


async def test_empty_base_hash_keeps_last_writer_wins(store: KBStore) -> None:
    path = "projects/alt/notes/blind.md"
    await store.kb_write(path, _concept("A."), "create")
    await store.kb_edit(path, "append", "external")
    # No base_hash → today's behavior: the write goes through regardless.
    res = await store.kb_write(path, _concept("B."), "update")
    assert res["no_change"] is False


# ------------------------------------------------------------------ claim / release / claims lifecycle


async def test_claim_creates_record_and_lists(store: KBStore, settings: Settings) -> None:
    res = await store.kb_claim("pc1-cc", "projects/alt/specs/api.md", note="editing the spec")
    assert res["path"] == "projects/alt/specs/api.md"
    assert res["session"] == "pc1-cc"
    assert "claimed_at" in res
    assert "already_claimed_by" not in res

    claims = await store.kb_claims()
    assert len(claims) == 1
    c = claims[0]
    assert c["session"] == "pc1-cc"
    assert c["path"] == "projects/alt/specs/api.md"
    assert c["note"] == "editing the spec"
    assert c["stale"] is False
    assert c["age_min"] is not None

    # Upsert: re-claiming the same path keeps one file.
    await store.kb_claim("pc1-cc", "projects/alt/specs/api.md")
    cdir = settings.brain_path / "workspace" / "claims"
    files = [f for f in cdir.glob("*.md") if f.name != "index.md"]
    assert len(files) == 1


async def test_claim_reports_foreign_active_holder(store: KBStore) -> None:
    await store.kb_claim("session-a", "projects/alt/specs/api.md")
    res = await store.kb_claim("session-b", "projects/alt/specs/api.md")
    assert res["already_claimed_by"] == "session-a"


async def test_claim_over_stale_foreign_has_no_holder(store: KBStore) -> None:
    old = _iso(_utcnow() - timedelta(minutes=_CLAIM_TTL_MIN + 5))
    _plant_claim(store.root, "projects/alt/specs/api.md", "session-a", old)
    _commit_all(store.root)
    res = await store.kb_claim("session-b", "projects/alt/specs/api.md")
    assert "already_claimed_by" not in res


async def test_claims_marks_stale_and_orders_active_first(store: KBStore) -> None:
    fresh = _iso(_utcnow())
    old = _iso(_utcnow() - timedelta(minutes=_CLAIM_TTL_MIN + 10))
    _plant_claim(store.root, "projects/alt/a.md", "sess-stale", old)
    _plant_claim(store.root, "projects/alt/b.md", "sess-fresh", fresh)
    claims = await store.kb_claims()
    assert claims[0]["session"] == "sess-fresh"
    assert claims[0]["stale"] is False
    assert claims[-1]["session"] == "sess-stale"
    assert claims[-1]["stale"] is True


async def test_release_removes_own_claim(store: KBStore, settings: Settings) -> None:
    await store.kb_claim("pc1-cc", "projects/alt/specs/api.md")
    res = await store.kb_release("pc1-cc", "projects/alt/specs/api.md")
    assert res["released"] is True
    cdir = settings.brain_path / "workspace" / "claims"
    assert not [f for f in cdir.glob("*.md") if f.name != "index.md"]
    assert await store.kb_claims() == []


async def test_release_foreign_claim_is_noop(store: KBStore) -> None:
    await store.kb_claim("session-a", "projects/alt/specs/api.md")
    res = await store.kb_release("session-b", "projects/alt/specs/api.md")
    assert res["released"] is False
    assert "note" in res
    # session-a's claim survives.
    assert len(await store.kb_claims()) == 1


async def test_release_nonexistent_is_noop(store: KBStore) -> None:
    res = await store.kb_release("pc1-cc", "projects/alt/never.md")
    assert res["released"] is False
    assert "nothing to release" in res["note"]


# ------------------------------------------------------------------ write/edit collision warning


async def test_kb_write_warns_on_foreign_active_claim(store: KBStore) -> None:
    await store.kb_claim("session-a", "projects/alt/notes/hot.md")
    res = await store.kb_write(
        "projects/alt/notes/hot.md", _concept(), "write", session="session-b"
    )
    assert any("session-a claimed this path" in w for w in res["warnings"])


async def test_kb_write_no_warn_for_own_claim(store: KBStore) -> None:
    await store.kb_claim("session-a", "projects/alt/notes/mine.md")
    res = await store.kb_write(
        "projects/alt/notes/mine.md", _concept(), "write", session="session-a"
    )
    assert not any("claimed this path" in w for w in res["warnings"])


async def test_kb_edit_warns_on_foreign_active_claim(store: KBStore) -> None:
    await store.kb_write("projects/alt/notes/edited.md", _concept(), "create")
    await store.kb_claim("session-a", "projects/alt/notes/edited.md")
    res = await store.kb_edit(
        "projects/alt/notes/edited.md", "append", "more", session="session-b"
    )
    assert any("session-a claimed this path" in w for w in res["warnings"])


async def test_stale_claim_does_not_warn(store: KBStore) -> None:
    old = _iso(_utcnow() - timedelta(minutes=_CLAIM_TTL_MIN + 5))
    _plant_claim(store.root, "projects/alt/notes/cold.md", "session-a", old)
    _commit_all(store.root)
    res = await store.kb_write(
        "projects/alt/notes/cold.md", _concept(), "write", session="session-b"
    )
    assert not any("claimed this path" in w for w in res["warnings"])


# ------------------------------------------------------------------ secret scan on thread / handoff


AWS = "AKIAIOSFODNN7EXAMPLE"


async def test_thread_post_blocks_secret(store: KBStore) -> None:
    with pytest.raises(KBError) as exc:
        await store.kb_thread_post("room-x", "session-a", f"the key is {AWS}")
    assert "AWS access key id" in str(exc.value)
    assert AWS not in str(exc.value)  # value never echoed


async def test_thread_post_allow_secrets_overrides(store: KBStore) -> None:
    res = await store.kb_thread_post(
        "room-y", "session-a", f"placeholder {AWS}", allow_secrets=True
    )
    assert res["seq"] == 0


async def test_handoff_blocks_secret_in_summary(store: KBStore) -> None:
    with pytest.raises(KBError, match="AWS access key id"):
        await store.kb_handoff("session-a", f"summary with {AWS}")


async def test_handoff_blocks_secret_in_next_steps(store: KBStore) -> None:
    with pytest.raises(KBError, match="AWS access key id"):
        await store.kb_handoff("session-a", "clean summary", next_steps=f"run with {AWS}")


async def test_handoff_allow_secrets_overrides(store: KBStore) -> None:
    res = await store.kb_handoff("session-a", f"summary {AWS}", allow_secrets=True)
    assert res["path"].startswith("workspace/handoffs/")


# ------------------------------------------------------------------ seq max+1 with a gap


async def test_thread_seq_is_max_plus_one_across_a_gap(store: KBStore) -> None:
    await store.kb_thread_post("gap-room", "a", "turn zero")
    await store.kb_thread_post("gap-room", "a", "turn one")
    await store.kb_thread_post("gap-room", "a", "turn two")
    turns_dir = store.root / "threads" / "gap-room" / "turns"
    # Corrupt the seq-1 turn so _read_turns drops it (a gap: seqs become 0, 2).
    for f in turns_dir.glob("*.md"):
        if f.name == "index.md":
            continue
        if read_meta(f).get("seq") == 1:
            f.write_text("not a valid concept\n", encoding="utf-8")
    _commit_all(store.root)
    res = await store.kb_thread_post("gap-room", "a", "turn three")
    # len(existing) would be 2 (collision with seq 2); max+1 must give 3.
    assert res["seq"] == 3


# ------------------------------------------------------------------ handoff notified flag


async def test_handoff_notified_true_when_room_posts(store: KBStore) -> None:
    res = await store.kb_handoff("session-a", "work", room="handoff-room")
    assert res["notified"] is True
    assert "room_error" not in res


async def test_handoff_notified_false_and_room_error_when_room_closed(store: KBStore) -> None:
    await store.kb_thread_post("closed-room", "session-x", "opening", close=True)
    res = await store.kb_handoff("session-a", "work", room="closed-room")
    assert res["notified"] is False
    assert "room_error" in res


async def test_handoff_notified_false_when_no_room(store: KBStore) -> None:
    res = await store.kb_handoff("session-a", "work")
    assert res["notified"] is False
    assert "room_error" not in res


# ------------------------------------------------------------------ refs-missing warning


async def test_thread_post_warns_on_missing_ref(store: KBStore) -> None:
    res = await store.kb_thread_post(
        "ref-room", "a", "here", refs=["projects/alt/nope.md"]
    )
    assert any("nope.md" in w and "exist" in w for w in res["warnings"])


async def test_thread_post_no_warn_for_existing_ref(store: KBStore) -> None:
    res = await store.kb_thread_post(
        "ref-room-2", "a", "here", refs=["projects/alt/context.md"]
    )
    assert res["warnings"] == []


# ------------------------------------------------------------------ all-hyphen / empty id rejection


async def test_all_hyphen_thread_id_rejected(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_thread_post("---", "a", "hi")


async def test_all_hyphen_claim_session_rejected(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_claim("---", "projects/alt/specs/api.md")


async def test_empty_alnum_claim_path_rejected(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_claim("session-a", "---")


async def test_all_hyphen_handoff_from_session_rejected(store: KBStore) -> None:
    with pytest.raises(KBError):
        await store.kb_handoff("---", "some summary")


# ------------------------------------------------------------------ roster no-filter (L4)


async def test_roster_nonpositive_window_disables_filter(store: KBStore) -> None:
    # Plant a stale presence record straight on disk.
    pdir = store.root / "workspace" / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    old = _iso(_utcnow() - timedelta(hours=5))
    (pdir / "old-sess.md").write_text(
        "---\ntype: presence\ntitle: 'Presence: old'\ndescription: x\n"
        f"session: old-sess\nstatus: working\nupdated: {old}\n---\n",
        encoding="utf-8",
    )
    assert await store.kb_roster(15) == []  # filtered out at the default window
    unfiltered = await store.kb_roster(0)  # <=0 disables the filter
    assert any(r["session"] == "old-sess" for r in unfiltered)
