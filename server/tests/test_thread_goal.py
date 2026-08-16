"""Item 4 — threads carry a goal and an exit condition.

Rooms have had both since they shipped, precisely so agent conversations
terminate instead of agreeing politely forever. Threads inherited the whole
turn-taking protocol but not this, which is why their turn cap could only warn
where a room's refuses: there was nothing to measure "finished" against.
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
    inv = registry.tenancy.create_invite("alice@example.com")
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "google", "google:alice@example.com")
    return registry


def _login(monkeypatch):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="google:alice@example.com"))


def _as_session(monkeypatch, key):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: key)


@pytest.mark.asyncio
async def test_a_thread_records_its_goal_and_exit_condition(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post(
        "aimed", "windows", "starting",
        goal="Decide the profile page layout",
        exit_condition="One layout picked and written to the brain",
    )
    read = await app_module.kb_thread_read("aimed", sender="windows")
    assert read["goal"] == "Decide the profile page layout"
    assert read["exit_condition"] == "One layout picked and written to the brain"


@pytest.mark.asyncio
async def test_a_joining_session_sees_what_the_thread_is_for(mu, monkeypatch):
    """The point of a goal is that someone arriving later knows the purpose
    without reading forty turns to infer it."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("aimed", "windows", "starting",
                                    goal="Ship the reskin")
    _as_session(monkeypatch, "sess-b")
    read = await app_module.kb_thread_read("aimed", sender="mac")
    assert read["goal"] == "Ship the reskin"


@pytest.mark.asyncio
async def test_the_goal_is_set_once_and_not_rewritten(mu, monkeypatch):
    """A goal you can edit mid-conversation becomes whatever the conversation
    drifted into — which is exactly the drift it exists to catch."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("fixed", "windows", "first", goal="The real goal")
    await app_module.kb_thread_post("fixed", "windows", "second",
                                    goal="A goal that suits where we ended up")
    read = await app_module.kb_thread_read("fixed", sender="windows")
    assert read["goal"] == "The real goal"


@pytest.mark.asyncio
async def test_the_turn_cap_quotes_the_exit_condition(mu, monkeypatch):
    """'40 turns in' is a scold. '40 turns in, and the exit condition was X' is a
    question the reader can answer."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_THREAD_TURN_BUDGET", 2)
    monkeypatch.setattr(app_module, "_THREAD_TURN_CAP", 99)
    await app_module.kb_thread_post(
        "long", "windows", "first", exit_condition="A layout is chosen",
    )
    for i in range(3):
        out = await app_module.kb_thread_post("long", "windows", f"turn {i}")
    warn = " ".join(out.get("warnings", []))
    assert "A layout is chosen" in warn
    assert "Is it met?" in warn


@pytest.mark.asyncio
async def test_a_goalless_thread_still_works(mu, monkeypatch):
    """Every thread that existed before today has neither field."""
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    monkeypatch.setattr(app_module, "_THREAD_TURN_BUDGET", 2)
    await app_module.kb_thread_post("plain", "windows", "first")
    for i in range(3):
        out = await app_module.kb_thread_post("plain", "windows", f"turn {i}")
    assert out["goal"] == ""
    warn = " ".join(out.get("warnings", []))
    assert "turns in" in warn
    assert "Is it met?" not in warn


@pytest.mark.asyncio
async def test_threads_listing_carries_the_goal(mu, monkeypatch):
    _login(monkeypatch)
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("listed", "windows", "hi", goal="Find the bug")
    rows = await app_module.kb_threads()
    row = next(r for r in rows if r["thread"] == "listed")
    assert row["goal"] == "Find the bug"
