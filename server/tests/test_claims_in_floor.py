"""Phase 1 — advisory claims surface where sessions already look.

kb_claim has existed since the workspace wave: advisory, 30-minute TTL, already
wired into kb_write/kb_edit. What was missing is that SEEING it required calling
kb_claims, and nothing ever prompted that. An advisory signal nobody fetches is
not a signal — so it rides in `floor`, the block every room and thread result
already returns.
"""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    s = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
    })
    registry = StoreRegistry(s)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", s)
    monkeypatch.setattr(app_module, "_presence_last", {})
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    return registry


def _login(monkeypatch, email="alice@example.com"):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


def _as_session(monkeypatch, key):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: key)


@pytest.mark.asyncio
async def test_a_room_turn_shows_who_is_working_where(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_claim("mac-a", "projects/vibechk/specs/theme.md", "reskin")
    await app_module.kb_room_open("build", "parallel work")
    out = await app_module.kb_room_post("build", "starting", speaker="windows")
    working = out["floor"]["working"]
    assert [w["path"] for w in working] == ["projects/vibechk/specs/theme.md"]
    assert working[0]["session"] == "mac-a"
    assert working[0]["note"] == "reskin"


@pytest.mark.asyncio
async def test_a_thread_turn_shows_it_too(mu, monkeypatch):
    """One protocol, two surfaces — the whole point of yesterday's unification."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_claim("mac-b", "server/engram_server/app.py", "floor work")
    out = await app_module.kb_thread_post("handoff", "windows", "starting")
    assert out["floor"]["working"][0]["session"] == "mac-b"


@pytest.mark.asyncio
async def test_reading_shows_it_without_speaking(mu, monkeypatch):
    """A session should see the workspace before it commits to anything."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_claim("mac-a", "docs/plan.md")
    await app_module.kb_room_open("build", "parallel work")
    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_room_read("build", speaker="mac")
    assert read["floor"]["working"][0]["path"] == "docs/plan.md"


@pytest.mark.asyncio
async def test_a_quiet_workspace_costs_nothing(mu, monkeypatch):
    """Absent rather than empty: this lands in an LLM's context every turn."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("quiet", "nobody has claimed anything")
    out = await app_module.kb_room_post("quiet", "hello", speaker="windows")
    assert "working" not in out["floor"]


@pytest.mark.asyncio
async def test_a_stale_claim_is_not_reported(mu, monkeypatch):
    """A claim past its TTL is not news, it's litter — and reporting it would
    train sessions to ignore the field."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_claim("mac-a", "docs/old.md")
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})

    real = (await app_module.current_store()).kb_claims

    async def _stale():
        rows = await real()
        return [{**r, "stale": True} for r in rows]

    monkeypatch.setattr((await app_module.current_store()), "kb_claims", _stale)
    await app_module.kb_room_open("quiet", "only a stale claim exists")
    out = await app_module.kb_room_post("quiet", "hello", speaker="windows")
    assert "working" not in out["floor"]


@pytest.mark.asyncio
async def test_claims_failing_never_breaks_the_conversation(mu, monkeypatch):
    """Coordination is a convenience; the conversation is the product."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})

    async def _boom():
        raise RuntimeError("claims store on fire")

    monkeypatch.setattr((await app_module.current_store()), "kb_claims", _boom)
    await app_module.kb_room_open("resilient", "claims are broken")
    out = await app_module.kb_room_post("resilient", "still works", speaker="windows")
    assert out["turn"]["id"]
    assert "working" not in out["floor"]


@pytest.mark.asyncio
async def test_the_list_is_capped(mu, monkeypatch):
    """It rides on every turn, so its cost must stay far below a collision's."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    for i in range(app_module._CLAIMS_IN_FLOOR + 4):
        await app_module.kb_claim("mac-a", f"docs/file-{i}.md")
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})
    await app_module.kb_room_open("busy", "many claims")
    out = await app_module.kb_room_post("busy", "hello", speaker="windows")
    assert len(out["floor"]["working"]) == app_module._CLAIMS_IN_FLOOR


# -- Phase 2: derived activity — working IS the announcement -------------------


@pytest.mark.asyncio
async def test_writing_announces_itself_without_a_claim(mu, monkeypatch):
    """The point of derivation: a session that never calls kb_claim is still
    visible, because the server already sees every write."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-b")
    monkeypatch.setattr(app_module, "_activity_last", {})
    await app_module.kb_write(
        "projects/p/notes/derived.md",
        "---\ntype: note\ndescription: d\n---\n\n# T\n\nBody.\n",
        "seed",
    )
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_room_open("build", "parallel work")
    out = await app_module.kb_room_post("build", "starting", speaker="windows")
    working = out["floor"]["working"]
    assert [w["path"] for w in working] == ["projects/p/notes/derived.md"]
    assert working[0]["via"] == "activity", "evidence, not stated intent"


@pytest.mark.asyncio
async def test_your_own_writes_are_not_reported_back_to_you(mu, monkeypatch):
    """You know what you are working on. What you cannot see is everyone else."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_activity_last", {})
    await app_module.kb_write(
        "projects/p/notes/mine.md",
        "---\ntype: note\ndescription: d\n---\n\n# T\n\nBody.\n",
        "seed",
    )
    await app_module.kb_room_open("solo", "just me")
    out = await app_module.kb_room_post("solo", "hello", speaker="windows")
    assert "working" not in out["floor"]


@pytest.mark.asyncio
async def test_a_claim_beats_derived_activity_on_the_same_path(mu, monkeypatch):
    """A claim carries the session's note, which says more than a write does."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-b")
    monkeypatch.setattr(app_module, "_activity_last", {})
    await app_module.kb_claim("mac-a", "projects/p/notes/both.md", "reskin")
    await app_module.kb_write(
        "projects/p/notes/both.md",
        "---\ntype: note\ndescription: d\n---\n\n# T\n\nBody.\n",
        "seed",
    )
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})
    await app_module.kb_room_open("build", "parallel work")
    out = await app_module.kb_room_post("build", "hi", speaker="windows")
    rows = [w for w in out["floor"]["working"]
            if w["path"] == "projects/p/notes/both.md"]
    assert len(rows) == 1, "one path, one entry — not both a claim and an echo"
    assert rows[0]["via"] == "claim"
    assert rows[0]["note"] == "reskin"


@pytest.mark.asyncio
async def test_activity_shows_the_name_a_session_gave_itself(mu, monkeypatch):
    """An opaque session key tells a reader nothing actionable."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-b")
    await app_module.kb_room_open("naming", "so sess-b has a name")
    await app_module.kb_room_post("naming", "hello", speaker="mac-b")
    monkeypatch.setattr(app_module, "_activity_last", {})
    await app_module.kb_write(
        "projects/p/notes/named.md",
        "---\ntype: note\ndescription: d\n---\n\n# T\n\nBody.\n",
        "seed",
    )
    _as_session(monkeypatch, "sess-a")
    out = await app_module.kb_room_post("naming", "and me", speaker="windows")
    assert out["floor"]["working"][0]["session"] == "mac-b"


@pytest.mark.asyncio
async def test_recording_activity_never_breaks_a_write(mu, monkeypatch):
    """Bookkeeping must not be able to fail the thing it is bookkeeping for."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_activity_last", {})

    def _boom(*a, **k):
        raise RuntimeError("activity store on fire")

    monkeypatch.setattr(mu.rooms, "record_activity", _boom)
    res = await app_module.kb_write(
        "projects/p/notes/safe.md",
        "---\ntype: note\ndescription: d\n---\n\n# T\n\nBody.\n",
        "seed",
    )
    assert res["path"] == "projects/p/notes/safe.md"
