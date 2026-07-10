"""Meeting widget: payload building over kb_threads/kb_thread_read, the
meeting_reply reply-only guard, app.py tool wiring under the ENGRAM_WIDGET flag,
and the widget HTML's UX contract (rooms list, transcript, reply box, polling
cadences, mobile-first affordances).
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.kbstore import KBStore
from engram_server.meetings_widget import (
    MEETINGS_HTML,
    MEETINGS_MIME,
    MEETINGS_URI,
    meeting_reply,
    meetings_app_tool_meta,
    meetings_payload,
    meetings_resource_meta,
    meetings_tool_meta,
    register_meetings_widget,
)

# --------------------------------------------------------------- meta helpers


def test_tool_meta_off_is_none() -> None:
    assert meetings_tool_meta(False) is None
    assert meetings_app_tool_meta(False) is None


def test_tool_meta_on_shapes() -> None:
    assert meetings_tool_meta(True) == {
        "ui": {"resourceUri": MEETINGS_URI, "visibility": ["model", "app"]}
    }
    assert meetings_app_tool_meta(True) == {"ui": {"visibility": ["app"]}}


def test_resource_meta_shape() -> None:
    meta = meetings_resource_meta()
    assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert meta["ui"]["prefersBorder"] is True
    assert meta["openai/widgetPrefersBorder"] is True


def test_mime_constant() -> None:
    assert MEETINGS_MIME == "text/html;profile=mcp-app"


# --------------------------------------------------------- resource registration


def test_register_off_no_resource() -> None:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-off")
    register_meetings_widget(mcp, False)
    resources = asyncio.run(mcp.list_resources())
    assert not any(str(r.uri) == MEETINGS_URI for r in resources)


def test_register_on_resource_present_with_meta() -> None:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-on")
    register_meetings_widget(mcp, True)
    resources = asyncio.run(mcp.list_resources())
    match = [r for r in resources if str(r.uri) == MEETINGS_URI]
    assert len(match) == 1
    assert match[0].mimeType == MEETINGS_MIME
    assert match[0].meta == meetings_resource_meta()


# --------------------------------------------------------------- payload building


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_payload_lists_open_threads_only(store: KBStore) -> None:
    await store.kb_thread_post("open-room", "session-a", "morning check-in")
    await store.kb_thread_post("closed-room", "session-a", "wrapping up", close=True)

    payload = await meetings_payload(store)
    threads = [t["thread"] for t in payload["threads"]]
    assert "open-room" in threads
    assert "closed-room" not in threads


async def test_payload_last_turn_and_needs_hiren_flag(store: KBStore) -> None:
    await store.kb_thread_post("plain-room", "session-a", "just chatting")
    await store.kb_thread_post("hiren-room", "session-a", "@hiren: which palette do you prefer?")

    payload = await meetings_payload(store)
    by_id = {t["thread"]: t for t in payload["threads"]}

    plain = by_id["plain-room"]
    assert plain["needs_hiren"] is False
    assert plain["last_turn"] == {
        "sender": "session-a",
        "message": "just chatting",
        "timestamp": plain["last_turn"]["timestamp"],
    }

    hiren_room = by_id["hiren-room"]
    assert hiren_room["needs_hiren"] is True
    assert hiren_room["last_turn"]["message"].startswith("@hiren")


async def test_needs_hiren_clears_once_hiren_replies(store: KBStore) -> None:
    await store.kb_thread_post("ask-room", "session-a", "@hiren: ship it or hold?")
    payload = await meetings_payload(store)
    assert next(t for t in payload["threads"] if t["thread"] == "ask-room")["needs_hiren"] is True

    await store.kb_thread_post("ask-room", "hiren", "ship it")
    payload = await meetings_payload(store)
    assert next(t for t in payload["threads"] if t["thread"] == "ask-room")["needs_hiren"] is False


async def test_payload_shape_matches_spec_keys(store: KBStore) -> None:
    await store.kb_thread_post("shape-room", "session-a", "hello", topic="Shape check")
    payload = await meetings_payload(store)
    row = next(t for t in payload["threads"] if t["thread"] == "shape-room")
    assert set(row.keys()) == {
        "thread", "topic", "status", "participants", "turn_count",
        "last_activity", "last_turn", "needs_hiren",
    }
    assert row["status"] == "open"
    assert row["topic"] == "Shape check"


# --------------------------------------------------------------- meeting_reply guard


async def test_reply_posts_as_hiren_regardless_of_caller_context(store: KBStore) -> None:
    await store.kb_thread_post("reply-room", "session-a", "@hiren: thoughts?")
    result = await meeting_reply(store, "reply-room", "looks good, ship it")
    assert result["participants"][-1] == "hiren" or "hiren" in result["participants"]

    read = await store.kb_thread_read("reply-room")
    assert read["turns"][-1]["sender"] == "hiren"
    assert read["turns"][-1]["message"] == "looks good, ship it"


async def test_reply_rejects_unknown_thread(store: KBStore) -> None:
    with pytest.raises(KBError, match="unknown thread"):
        await meeting_reply(store, "never-existed", "hello?")


async def test_reply_rejects_closed_thread(store: KBStore) -> None:
    await store.kb_thread_post("done-room", "session-a", "wrapping up", close=True)
    with pytest.raises(KBError, match="closed"):
        await meeting_reply(store, "done-room", "one more thing")


async def test_reply_never_creates_or_reopens(store: KBStore) -> None:
    # A thread that never existed must stay nonexistent — meeting_reply is reply-only.
    with pytest.raises(KBError):
        await meeting_reply(store, "brand-new-room", "hi")
    read = await store.kb_thread_read("brand-new-room")
    assert read["status"] == "none"


async def test_reply_rejects_empty_message(store: KBStore) -> None:
    await store.kb_thread_post("empty-room", "session-a", "hello")
    with pytest.raises(KBError):
        await meeting_reply(store, "empty-room", "   ")


async def test_reply_rejects_over_length_cap(store: KBStore) -> None:
    await store.kb_thread_post("long-room", "session-a", "hello")
    with pytest.raises(KBError, match="too long"):
        await meeting_reply(store, "long-room", "x" * 4001)


async def test_reply_is_secret_scanned(store: KBStore) -> None:
    await store.kb_thread_post("secret-room", "session-a", "hello")
    with pytest.raises(KBError):
        await meeting_reply(store, "secret-room", "here's the key AKIAABCDEFGHIJKLMNOP")


# ---------------------------------------------------- app.py wiring (flag on/off)


def _reload_app(monkeypatch, *, widget: bool):
    from engram_server import config

    monkeypatch.setenv("ENGRAM_WIDGET", "1" if widget else "0")
    config.get_settings.cache_clear()
    import engram_server.app as app_module

    return importlib.reload(app_module)


def test_app_flag_off_meetings_tools_have_no_meta(monkeypatch) -> None:
    app_module = _reload_app(monkeypatch, widget=False)
    try:
        tools = {t.name: t for t in asyncio.run(app_module.mcp.list_tools())}
        for name in ("kb_meetings", "meetings_state", "meeting_transcript", "meeting_reply"):
            assert tools[name].meta is None, name
        resources = asyncio.run(app_module.mcp.list_resources())
        assert not any(str(r.uri) == MEETINGS_URI for r in resources)
    finally:
        _reload_app(monkeypatch, widget=False)


def test_app_flag_on_wires_meetings_tools_and_resource(monkeypatch) -> None:
    app_module = _reload_app(monkeypatch, widget=True)
    try:
        tools = {t.name: t for t in asyncio.run(app_module.mcp.list_tools())}
        assert tools["kb_meetings"].meta == {
            "ui": {"resourceUri": MEETINGS_URI, "visibility": ["model", "app"]}
        }
        for name in ("meetings_state", "meeting_transcript", "meeting_reply"):
            assert tools[name].meta == {"ui": {"visibility": ["app"]}}, name
        resources = asyncio.run(app_module.mcp.list_resources())
        assert any(str(r.uri) == MEETINGS_URI for r in resources)
    finally:
        _reload_app(monkeypatch, widget=False)


def test_every_meetings_tool_has_a_nonempty_docstring(monkeypatch) -> None:
    app_module = _reload_app(monkeypatch, widget=True)
    try:
        tools = {t.name: t for t in asyncio.run(app_module.mcp.list_tools())}
        for name in ("kb_meetings", "meetings_state", "meeting_transcript", "meeting_reply"):
            assert (tools[name].description or "").strip(), name
    finally:
        _reload_app(monkeypatch, widget=False)


# ------------------------------------------------------------------ HTML sanity


def test_html_is_self_contained_mcp_app() -> None:
    assert MEETINGS_HTML.startswith("<!DOCTYPE html>")
    assert "<script" in MEETINGS_HTML


def test_html_has_no_external_requests() -> None:
    assert 'src="http' not in MEETINGS_HTML
    assert 'href="http' not in MEETINGS_HTML
    assert "https://" not in MEETINGS_HTML
    assert "http://" not in MEETINGS_HTML


def test_html_rooms_list_view() -> None:
    assert 'callTool("kb_meetings"' in MEETINGS_HTML
    assert 'callTool("meetings_state"' in MEETINGS_HTML
    assert "No meetings right now — agents open rooms with kb_thread_post." in MEETINGS_HTML
    assert "needs-badge" in MEETINGS_HTML
    assert "needs you" in MEETINGS_HTML


def test_html_transcript_view() -> None:
    assert 'callTool("meeting_transcript"' in MEETINGS_HTML
    assert "An agent is waiting on you — reply below" in MEETINGS_HTML
    assert "function openTranscript(" in MEETINGS_HTML
    assert "function appendTurns(" in MEETINGS_HTML


def test_html_reply_box() -> None:
    assert 'callTool("meeting_reply"' in MEETINGS_HTML
    assert "function sendReply(" in MEETINGS_HTML
    # Enter sends (no shift)
    assert 'e.key==="Enter" && !e.shiftKey' in MEETINGS_HTML
    # optimistic bubble + content-key dedupe against the next poll
    assert "bubble pending" in MEETINGS_HTML
    assert "seenContent" in MEETINGS_HTML
    # inline error + text restore on failure
    assert "Couldn't send — try again." in MEETINGS_HTML
    assert "inp.value=text" in MEETINGS_HTML


def test_html_polling_cadences_and_stop_when_hidden() -> None:
    assert "setInterval(pollList,5000)" in MEETINGS_HTML
    assert "setInterval(pollTranscript,2000)" in MEETINGS_HTML
    assert "function stopAllPolling(" in MEETINGS_HTML
    assert "visibilitychange" in MEETINGS_HTML
    assert "document.hidden" in MEETINGS_HTML


def test_html_stops_polling_on_teardown() -> None:
    assert '"ui/resource-teardown"' in MEETINGS_HTML
    assert "stopAllPolling();" in MEETINGS_HTML


def test_html_requests_fullscreen_entering_transcript() -> None:
    assert "ui/request-display-mode" in MEETINGS_HTML
    assert "fullscreen" in MEETINGS_HTML


def test_html_mobile_first_affordances() -> None:
    # generous tap targets on the room card and the round send button
    assert 'width:2.6rem; height:2.6rem' in MEETINGS_HTML  # send button size
    assert "viewport" in MEETINGS_HTML and "width=device-width" in MEETINGS_HTML


def test_html_unwraps_sdk_result_envelope() -> None:
    assert "function unwrap(" in MEETINGS_HTML
    assert "unwrap(pr.structuredContent" in MEETINGS_HTML


def test_html_jsonrpc_and_appinfo_rails() -> None:
    assert 'jsonrpc!=="2.0"' in MEETINGS_HTML
    assert "appInfo" in MEETINGS_HTML
    assert "clientInfo" not in MEETINGS_HTML
