"""Office widget: meta shapes, resource registration (incl. the 3 floor-art
sub-resources served over resources/read), the office_summary() compact-roster
projection, end-to-end tool wiring/calls over a real KBStore, and the widget
HTML's UX contract (never-blank floor fallback, ported procedural renderer,
polling/animation lifecycle, meeting-room tap-to-open).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.office_widget import (
    OFFICE_BACK_URI,
    OFFICE_FRONT_URI,
    OFFICE_HTML,
    OFFICE_MANIFEST_URI,
    OFFICE_MIME,
    OFFICE_URI,
    _FLOOR_DIR,
    office_resource_meta,
    office_state_tool_meta,
    office_summary,
    office_tool_meta,
    register_office_widget,
)

# --------------------------------------------------------------- meta helpers


def test_tool_meta_off_is_none() -> None:
    assert office_tool_meta(False) is None
    assert office_state_tool_meta(False) is None


def test_tool_meta_on_shapes() -> None:
    assert office_tool_meta(True) == {
        "ui": {"resourceUri": OFFICE_URI, "visibility": ["model", "app"]}
    }
    # app-only: no resourceUri (mounts nothing — the widget is already up)
    assert office_state_tool_meta(True) == {"ui": {"visibility": ["app"]}}
    assert "resourceUri" not in office_state_tool_meta(True)["ui"]


def test_resource_meta_shape() -> None:
    meta = office_resource_meta()
    assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert meta["ui"]["prefersBorder"] is True
    assert meta["openai/widgetPrefersBorder"] is True


def test_mime_constant() -> None:
    assert OFFICE_MIME == "text/html;profile=mcp-app"


def test_no_flat_resource_uri_key() -> None:
    for meta in (office_tool_meta(True), office_state_tool_meta(True)):
        assert "ui/resourceUri" not in meta


# --------------------------------------------------------------- office_summary()


def _fake_payload(**overrides) -> dict:
    base = {
        "now": "2026-07-10T12:00:00Z",
        "sessions": [
            {"session": "s1", "name": "tower/aeos", "status": "working",
             "project": "aeos", "repo": "aeos", "room_label": "aeos", "quiet": False},
            {"session": "s2", "name": "lap/engram", "status": "idle",
             "project": "", "repo": "engram", "room_label": "engram", "quiet": True},
        ],
        "recent": [],
        "rooms": [{"id": "aeos", "label": "aeos", "kind": "project"}],
        "threads": [
            {"thread": "roll-call", "status": "open", "topic": "", "participants": ["s1"],
             "last_turn": {"sender": "s1", "message": "morning", "timestamp": "2026-07-10T11:00:00Z"}},
            {"thread": "ask-hiren", "status": "open", "topic": "", "participants": ["s2"],
             "last_turn": {"sender": "s2", "message": "@hiren: ship it?", "timestamp": "2026-07-10T11:05:00Z"}},
            {"thread": "old-room", "status": "closed", "topic": "", "participants": [],
             "last_turn": None},
        ],
        "activity": [],
    }
    base.update(overrides)
    return base


def test_summary_counts_and_shape() -> None:
    s = office_summary(_fake_payload())
    assert s["online"] == 2
    assert len(s["sessions"]) == 2
    assert s["sessions"][0] == {
        "name": "tower/aeos", "status": "working", "project": "aeos", "room": "aeos", "quiet": False
    }
    assert s["rooms"] == ["aeos"]
    # only OPEN threads count — the closed one is excluded
    assert s["meetings_open"] == 2


def test_summary_needs_hiren_true_when_open_turn_addresses_him() -> None:
    s = office_summary(_fake_payload())
    assert s["needs_hiren"] is True


def test_summary_needs_hiren_false_when_no_open_turn_addresses_him() -> None:
    payload = _fake_payload()
    payload["threads"] = [t for t in payload["threads"] if t["thread"] != "ask-hiren"]
    s = office_summary(payload)
    assert s["needs_hiren"] is False


def test_summary_project_falls_back_to_repo() -> None:
    s = office_summary(_fake_payload())
    assert s["sessions"][1]["project"] == "engram"  # no project set -> repo


def test_summary_handles_empty_payload() -> None:
    s = office_summary({})
    assert s == {
        "now": None, "online": 0, "sessions": [], "rooms": [],
        "meetings_open": 0, "needs_hiren": False,
    }


# --------------------------------------------------------------- resource registration


def test_register_off_no_resources_or_tools() -> None:
    mcp = FastMCP("test-office-off")
    settings = Settings(_env_file=None, widget=False)

    class _StubStore:
        root = None

    register_office_widget(mcp, settings, _StubStore())
    resources = asyncio.run(mcp.list_resources())
    assert not any(str(r.uri) in (OFFICE_URI, OFFICE_BACK_URI, OFFICE_FRONT_URI, OFFICE_MANIFEST_URI) for r in resources)
    tools = asyncio.run(mcp.list_tools())
    assert not any(t.name in ("kb_office", "office_state") for t in tools)


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _widget_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"widget": True})


def test_register_on_wires_html_resource(settings: Settings, store: KBStore) -> None:
    mcp = FastMCP("test-office-on")
    register_office_widget(mcp, _widget_settings(settings), store)
    resources = asyncio.run(mcp.list_resources())
    match = [r for r in resources if str(r.uri) == OFFICE_URI]
    assert len(match) == 1
    assert match[0].mimeType == OFFICE_MIME
    assert match[0].meta == office_resource_meta()


def test_register_on_wires_floor_art_sub_resources(settings: Settings, store: KBStore) -> None:
    mcp = FastMCP("test-office-art")
    register_office_widget(mcp, _widget_settings(settings), store)
    resources = {str(r.uri): r for r in asyncio.run(mcp.list_resources())}
    assert resources[OFFICE_BACK_URI].mimeType == "image/png"
    assert resources[OFFICE_FRONT_URI].mimeType == "image/png"
    assert resources[OFFICE_MANIFEST_URI].mimeType == "application/json"


def test_back_png_resource_serves_raw_bake_bytes(settings: Settings, store: KBStore) -> None:
    # FastMCP.read_resource() hands back the resource function's raw return value
    # (base64 blob-encoding happens one layer down, in the lowlevel protocol
    # handler) — so a bytes-returning resource must round-trip the exact PNG bytes.
    mcp = FastMCP("test-office-back")
    register_office_widget(mcp, _widget_settings(settings), store)
    contents = asyncio.run(mcp.read_resource(OFFICE_BACK_URI))
    on_disk = (_FLOOR_DIR / "office_back.png").read_bytes()
    assert len(contents) == 1
    assert contents[0].content == on_disk
    assert contents[0].mime_type == "image/png"


def test_manifest_resource_serves_valid_json_with_desks(settings: Settings, store: KBStore) -> None:
    mcp = FastMCP("test-office-manifest")
    register_office_widget(mcp, _widget_settings(settings), store)
    contents = asyncio.run(mcp.read_resource(OFFICE_MANIFEST_URI))
    body = "".join(c.content for c in contents)
    data = json.loads(body)
    assert "desks" in data and len(data["desks"]) == 8
    assert "confRooms" in data


# --------------------------------------------------------------- tool wiring + calls


def test_registered_tools_carry_expected_meta(settings: Settings, store: KBStore) -> None:
    mcp = FastMCP("test-office-tools")
    register_office_widget(mcp, _widget_settings(settings), store)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert tools["kb_office"].meta == office_tool_meta(True)
    assert tools["office_state"].meta == office_state_tool_meta(True)
    for name in ("kb_office", "office_state"):
        assert (tools[name].description or "").strip(), name


async def _call_tool_json(mcp: FastMCP, name: str, args: dict | None = None) -> dict:
    result = await mcp.call_tool(name, args or {})
    # This SDK version returns (content_list, structured_content) — the structured
    # dict is the direct value, no text-parsing needed. Fall back to parsing the
    # text content for SDK versions that return a bare content list instead.
    if isinstance(result, tuple):
        _content, structured = result
        return structured
    text = next(c.text for c in result if c.type == "text")
    return json.loads(text)


async def test_kb_office_returns_compact_summary_end_to_end(settings: Settings, store: KBStore) -> None:
    await store.kb_presence("s1", name="tower/aeos", status="working", project="aeos")
    await store.kb_thread_post("ask-hiren", "s1", "@hiren: ship it or hold?")

    mcp = FastMCP("test-kb-office")
    register_office_widget(mcp, _widget_settings(settings), store)
    data = await _call_tool_json(mcp, "kb_office")

    assert data["online"] >= 1
    assert any(s["name"] == "tower/aeos" for s in data["sessions"])
    assert data["needs_hiren"] is True
    assert data["meetings_open"] >= 1
    # compact — NOT the full render payload (no rooms/desks geometry)
    assert "desks" not in data and "activity" not in data


async def test_office_state_returns_full_render_payload(settings: Settings, store: KBStore) -> None:
    await store.kb_presence("s2", name="lap/engram", status="idle", project="engram")

    mcp = FastMCP("test-office-state")
    register_office_widget(mcp, _widget_settings(settings), store)
    data = await _call_tool_json(mcp, "office_state")

    assert {"now", "sessions", "recent", "rooms", "threads", "activity"} <= set(data.keys())
    assert any(s["name"] == "lap/engram" for s in data["sessions"])


async def test_needs_hiren_clears_once_hiren_replies(settings: Settings, store: KBStore) -> None:
    await store.kb_thread_post("ask-room", "s1", "@hiren: which one?")
    mcp = FastMCP("test-office-clear")
    register_office_widget(mcp, _widget_settings(settings), store)
    before = await _call_tool_json(mcp, "kb_office")
    assert before["needs_hiren"] is True

    await store.kb_thread_post("ask-room", "hiren", "the first one")
    after = await _call_tool_json(mcp, "kb_office")
    assert after["needs_hiren"] is False


# ------------------------------------------------------------------ HTML sanity / rails


def test_html_is_self_contained_mcp_app() -> None:
    assert OFFICE_HTML.startswith("<!DOCTYPE html>")
    assert "<script" in OFFICE_HTML


def test_html_has_no_external_requests() -> None:
    assert 'src="http' not in OFFICE_HTML
    assert 'href="http' not in OFFICE_HTML
    assert "https://" not in OFFICE_HTML
    assert "http://" not in OFFICE_HTML


def test_html_jsonrpc_and_appinfo_rails() -> None:
    assert 'jsonrpc!=="2.0"' in OFFICE_HTML
    assert 'jsonrpc:"2.0"' in OFFICE_HTML
    assert "appInfo" in OFFICE_HTML
    assert "clientInfo" not in OFFICE_HTML


def test_html_initialize_handshake_sequence() -> None:
    assert '"ui/initialize"' in OFFICE_HTML
    assert "ui/notifications/initialized" in OFFICE_HTML


def test_html_reports_height_both_channels() -> None:
    assert "notifyIntrinsicHeight" in OFFICE_HTML
    assert "ui/notifications/size-changed" in OFFICE_HTML


def test_html_teardown_is_acknowledged_and_stops_everything() -> None:
    assert '"ui/resource-teardown"' in OFFICE_HTML
    assert "stopAll();" in OFFICE_HTML
    assert "id:m.id,result:{}" in OFFICE_HTML


def test_html_unwraps_sdk_result_envelope() -> None:
    assert "function unwrap(" in OFFICE_HTML
    assert "unwrap(pr.structuredContent" in OFFICE_HTML


def test_html_loads_floor_art_over_resources_read() -> None:
    assert 'readResource("ui://engram/office/manifest.json")' in OFFICE_HTML
    assert '"ui://engram/office/back.png"' in OFFICE_HTML
    assert '"ui://engram/office/front.png"' in OFFICE_HTML
    assert '"resources/read"' in OFFICE_HTML
    # base64 blob -> data URL -> Image, never a direct fetch
    assert "c.blob" in OFFICE_HTML
    assert 'img.src="data:"' in OFFICE_HTML


def test_html_never_blank_floor_fallback() -> None:
    # embedded manifest default + flat-shape fallback drawing when the bake fails
    assert '"world":{"w":544,"h":352}' in OFFICE_HTML
    assert "function drawFlatBack(" in OFFICE_HTML
    assert "function drawFlatFront(" in OFFICE_HTML
    assert "function drawFloorLayer(" in OFFICE_HTML


def test_html_ports_procedural_character_renderer() -> None:
    # ported straight from office.html, not imported at runtime
    assert "function drawBody(" in OFFICE_HTML
    assert "function paletteFor(" in OFFICE_HTML
    assert "STATUS_COL" in OFFICE_HTML
    assert "function drawNamePill(" in OFFICE_HTML


def test_html_calls_kb_office_and_office_state() -> None:
    assert 'callTool("office_state"' in OFFICE_HTML
    # kb_office is called by the model to mount — the widget itself only polls
    # office_state (app-only, zero context cost); kb_office is not re-called client-side.
    assert 'callTool("office_state",{})' in OFFICE_HTML


def test_html_polling_cadence_and_stop_when_hidden() -> None:
    assert "setInterval(refreshState,5000)" in OFFICE_HTML
    assert "function stopAll(" in OFFICE_HTML
    assert "visibilitychange" in OFFICE_HTML
    assert "document.hidden" in OFFICE_HTML


def test_html_needs_hiren_badge_and_conf_room_states() -> None:
    assert "needs Hiren" in OFFICE_HTML
    assert "z z z" in OFFICE_HTML  # empty room = asleep
    assert "function drawConfRooms(" in OFFICE_HTML


def test_html_meeting_room_tap_suggests_open_in_meetings_widget() -> None:
    assert "canvas.addEventListener(\"click\"" in OFFICE_HTML
    assert "askAgent(" in OFFICE_HTML
    assert "Open the meeting" in OFFICE_HTML


def test_html_mobile_friendly_viewport() -> None:
    assert "viewport" in OFFICE_HTML and "width=device-width" in OFFICE_HTML


def test_html_status_legend_present() -> None:
    for label in ("working", "idle", "blocked", "available"):
        assert label in OFFICE_HTML
