"""kb_threads(wait_seconds) — one listener call watching every thread at once.

The gap this closes: kb_rooms could already park across all rooms, but threads —
the surface a single user with several machines actually uses — could not. A
session had to pick ONE thread to block on and went deaf to the rest, or it
polled, which is the thing every part of this design exists to prevent.

Also covers the turn-awareness hook: a Claude Code session that ended its turn
cannot be woken, so every user prompt doubles as the checkpoint where it learns
"someone is waiting on you" — from the local cache, with zero network.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
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
                                   "github", "github:alice")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="github:alice"))
    return registry


def _as_session(monkeypatch, key):
    monkeypatch.setattr(app_module, "_speaker_key", lambda: key)


@pytest.mark.asyncio
async def test_listing_still_lists_and_annotates_open_threads(mu, monkeypatch):
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("alpha", "windows", "opening alpha")
    await app_module.kb_thread_post("beta", "windows", "opening beta")

    out = await app_module.kb_threads()
    names = {t["thread"] for t in out["threads"]}
    assert {"alpha", "beta"} <= names
    a = next(t for t in out["threads"] if t["thread"] == "alpha")
    # Old fields survive; new ones appear on open threads.
    assert "topic" in a and "participants" in a
    assert "your_turn" in a and "do_next" in a


@pytest.mark.asyncio
async def test_it_returns_immediately_when_a_floor_is_waiting_on_you(mu, monkeypatch):
    """Parking on top of a floor handed to you is how a session misses the message
    it was called to read. `waiting_on_you` is the deal-with-first list."""
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("handed", "windows", "over to you")
    _as_session(monkeypatch, "sess-b")
    # Reading claims the open floor — the same path a real joiner takes.
    await app_module.kb_thread_read("handed", sender="mac")

    started = time.monotonic()
    out = await app_module.kb_threads(wait_seconds=25)
    elapsed = time.monotonic() - started
    assert "handed" in out["waiting_on_you"]
    assert elapsed < 2.0, "must not park while something waits on you"


@pytest.mark.asyncio
async def test_a_parked_listener_wakes_on_any_thread(mu, monkeypatch):
    """The point of the tool: ONE call watches every open thread, and the wake is
    the bus, not a timer — asserted as latency, since a wake that arrived on a
    poll tick would pass any correctness-only test."""
    _as_session(monkeypatch, "sess-a")
    await app_module.kb_thread_post("watch-one", "windows", "seed one")
    await app_module.kb_thread_post("watch-two", "windows", "seed two")
    _as_session(monkeypatch, "sess-b")
    # Catch up AND SPEAK in both, handing the floor back to windows. This is the
    # honest listener state: nothing unread, floor with the other side. The first
    # version of this test skipped the speak — whereupon mac HELD both floors and
    # kb_threads correctly refused to park (a floor-holder that parks is the
    # deadlock the floor exists to prevent) and returned immediately. The tool
    # was right; the scenario was wrong.
    await app_module.kb_thread_read("watch-one", sender="mac")
    await app_module.kb_thread_post("watch-one", "mac", "ack one")
    await app_module.kb_thread_read("watch-two", sender="mac")
    await app_module.kb_thread_post("watch-two", "mac", "ack two")
    # Warm the listing (git refresh, shadow rooms) so the timed call below parks
    # almost immediately — otherwise a slow first annotation can outlast the
    # speaker's delay and the turn lands in the (bounded) lost-wakeup window,
    # which is a different behaviour than the park this test asserts.
    await app_module.kb_threads()

    async def _speak_soon():
        # Long enough that the (warmed) listener is certainly parked.
        await anyio.sleep(1.0)
        _as_session(monkeypatch, "sess-a")
        await app_module.kb_thread_post("watch-two", "windows", "it happened here")

    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(_speak_soon)
        _as_session(monkeypatch, "sess-b")
        out = await app_module.kb_threads(wait_seconds=25)
    elapsed = time.monotonic() - started

    assert out["woke_on"] == "watch-two"
    assert elapsed < 4.0, f"woke on a clock, not the bus ({elapsed:.2f}s)"
    two = next(t for t in out["threads"] if t["thread"] == "watch-two")
    assert two["unread"] >= 1, "the re-list must show the state that woke us"


def _run_turns_hook(tmp_path, cache: dict | None, age: float = 0.0) -> str:
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    if cache is not None:
        cache = dict(cache)
        cache["at"] = time.time() - age
        (home / ".engram" / "status-cache.json").write_text(
            json.dumps(cache), encoding="utf-8")
    hook = Path(__file__).resolve().parents[2] / "hooks" / "engram_turns_hook.py"
    out = subprocess.run(
        [sys.executable, str(hook)],
        capture_output=True, encoding="utf-8", timeout=15,
        env={"HOME": str(home), "USERPROFILE": str(home),
             "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
    )
    assert out.returncode == 0, "the hook must NEVER fail a prompt"
    return out.stdout


def test_the_turns_hook_surfaces_a_waiting_thread(tmp_path):
    out = _run_turns_hook(tmp_path, {"your_turn": ["pi-harness-setup"],
                                     "needs_you": []})
    assert "[engram]" in out and "pi-harness-setup" in out


def test_the_turns_hook_is_silent_when_nothing_waits(tmp_path):
    assert _run_turns_hook(tmp_path, {"your_turn": [], "needs_you": []}) == ""


def test_the_turns_hook_is_silent_on_a_stale_cache(tmp_path):
    """A stale cache may describe a turn someone already took. Saying nothing
    beats reporting a world that has moved on."""
    assert _run_turns_hook(
        tmp_path, {"your_turn": ["old-thread"], "needs_you": []}, age=600) == ""


def test_the_turns_hook_is_silent_with_no_cache_at_all(tmp_path):
    assert _run_turns_hook(tmp_path, None) == ""
