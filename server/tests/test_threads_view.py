"""The live cross-session thread viewer: /brain/threads (list) and
/brain/threads/{id} (auto-refreshing transcript).

The threads/ tree is written directly on disk here (the kb_thread_* engine that
produces it is a separate concern) — these tests exercise ONLY the explorer's
defensive rendering: empty state, ordering, the open/closed meta-refresh switch,
the traversal guard, and the Cloudflare Access gate.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.config import Settings
from engram_server.explorer import register as register_explorer


def _client(settings: Settings) -> TestClient:
    mcp = FastMCP("test-threads")
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


def _open(settings: Settings) -> Settings:
    """Same settings with the Access gate disabled (dev-only bypass)."""
    return settings.model_copy(update={"dev_no_access": True})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _seed_thread(
    brain: Path,
    tid: str,
    *,
    status: str = "open",
    topic: str = "Shipping the widget",
    participants: tuple[str, ...] = ("claude-code", "claude-ai"),
    turns: tuple[tuple[str, int, str], ...] = (),
    closed_by: str | None = None,
) -> None:
    """Seed threads/<tid>/thread.md + turns/*.md. ``turns`` items are
    (sender, seq, body); the turn timestamp is derived from seq."""
    tdir = brain / "threads" / tid
    fm = [
        "---",
        "type: thread",
        f"status: {status}",
        f"topic: {topic}",
        "created: 2026-07-09T09:00:00+00:00",
        "participants:",
        *[f"  - {p}" for p in participants],
    ]
    if closed_by:
        fm.append(f"closed_by: {closed_by}")
        fm.append("closed_at: 2026-07-09T18:00:00+00:00")
    fm += ["---", "", f"# {topic}", ""]
    _write(tdir / "thread.md", "\n".join(fm) + "\n")

    for sender, seq, body in turns:
        ts = f"2026-07-09T10:{seq:02d}:00+00:00"
        turn = (
            f"---\ntype: thread-turn\nsender: {sender}\ntimestamp: {ts}\nseq: {seq}\n---\n\n{body}\n"
        )
        # colons are illegal in Windows filenames — the on-disk name is fs-safe;
        # the frontmatter timestamp (colons intact) is what the viewer reads.
        safe = ts.replace(":", "").replace("+", "")
        _write(tdir / "turns" / f"{safe}-{sender}.md", turn)


# ------------------------------------------------------------ list view


def test_threads_list_shows_seeded_thread(settings: Settings) -> None:
    brain = settings.brain_path
    _seed_thread(
        brain,
        "shipping-the-widget",
        turns=(("claude-code", 1, "Kicking off."), ("claude-ai", 2, "On it.")),
    )
    resp = _client(_open(settings)).get("/brain/threads")
    assert resp.status_code == 200
    html = resp.text
    assert "Shipping the widget" in html
    assert "claude-code" in html and "claude-ai" in html  # participant chips
    assert "2 turns" in html
    assert "badge active" in html  # open → green status badge
    # links through to the live view
    assert "/brain/threads/shipping-the-widget" in html


def test_threads_list_empty_state_when_no_tree(settings: Settings) -> None:
    # brain_path has no threads/ directory at all.
    resp = _client(_open(settings)).get("/brain/threads")
    assert resp.status_code == 200
    assert "No cross-session threads yet" in resp.text
    assert "kb_thread_post" in resp.text


def test_threads_list_closed_badge_is_not_green(settings: Settings) -> None:
    _seed_thread(settings.brain_path, "wrapped-up", status="closed", closed_by="claude-ai")
    html = _client(_open(settings)).get("/brain/threads").text
    # a closed thread's status badge is the plain (grey) variant, not `badge active`
    assert ">closed<" in html


# ------------------------------------------------------------ transcript view


def test_thread_view_renders_turns_in_seq_order_with_live_refresh(settings: Settings) -> None:
    # seq drives order even when the filename timestamp sorts the other way.
    brain = settings.brain_path
    tdir = brain / "threads" / "ordered"
    _write(
        tdir / "thread.md",
        "---\ntype: thread\nstatus: open\ntopic: Ordered\nparticipants:\n"
        "  - alice\n  - bob\n---\n\n# Ordered\n",
    )
    _write(
        tdir / "turns" / "2026-07-09T120000-alice.md",
        "---\ntype: thread-turn\nsender: alice\ntimestamp: 2026-07-09T12:00:00+00:00\nseq: 1\n---\n\nFIRST message.\n",
    )
    _write(
        tdir / "turns" / "2026-07-09T110000-bob.md",  # earlier filename, later seq
        "---\ntype: thread-turn\nsender: bob\ntimestamp: 2026-07-09T11:00:00+00:00\nseq: 2\n---\n\nSECOND message.\n",
    )
    resp = _client(_open(settings)).get("/brain/threads/ordered")
    assert resp.status_code == 200
    html = resp.text
    assert "FIRST message." in html and "SECOND message." in html
    assert html.index("FIRST message.") < html.index("SECOND message.")  # seq order
    # chat bubbles, alternating sides
    assert "bubble left" in html and "bubble right" in html
    # OPEN → the meta refresh + live indicator are present
    assert '<meta http-equiv="refresh" content="2">' in html
    assert 'class="livebadge"' in html  # the rendered indicator, not just the CSS rule


def test_thread_view_closed_omits_refresh_and_shows_ender(settings: Settings) -> None:
    _seed_thread(
        settings.brain_path,
        "done-deal",
        status="closed",
        closed_by="claude-code",
        turns=(("claude-code", 1, "All set."),),
    )
    resp = _client(_open(settings)).get("/brain/threads/done-deal")
    assert resp.status_code == 200
    html = resp.text
    assert "All set." in html
    # CLOSED → no auto-refresh, no live indicator, an ender footer instead
    assert 'http-equiv="refresh"' not in html
    assert 'class="livebadge"' not in html  # no rendered live indicator (CSS rule may still exist)
    assert "Conversation ended by claude-code" in html


def test_thread_view_unknown_thread_is_friendly_404(settings: Settings) -> None:
    _seed_thread(settings.brain_path, "real-one")  # a tree exists, but not this id
    resp = _client(_open(settings)).get("/brain/threads/no-such-thread")
    assert resp.status_code == 404
    assert "No such thread" in resp.text


def test_thread_view_traversal_and_bad_ids_rejected(settings: Settings) -> None:
    _seed_thread(settings.brain_path, "real-one")
    client = _client(_open(settings))
    # non-kebab ids never resolve to a path — a friendly 404, never a traversal.
    # (".." is normalized away by the client before it reaches routing; the regex
    # guard covers ids that DO reach the handler — leading dot, shell/space chars.)
    for bad in ("foo$bar", ".hidden", "a b", "..%2f..%2fetc"):
        assert client.get(f"/brain/threads/{bad}").status_code == 404, bad


# ------------------------------------------------------------ access gate


def test_thread_routes_stay_guarded(settings: Settings) -> None:
    _seed_thread(settings.brain_path, "real-one")
    client = _client(settings)  # dev_no_access False → Access gate holds
    assert client.get("/brain/threads").status_code == 403
    assert client.get("/brain/threads/real-one").status_code == 403
