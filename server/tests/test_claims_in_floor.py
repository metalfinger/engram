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
