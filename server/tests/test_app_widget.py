"""Unified app widget: meta shapes, resource registration, the HTML size budget,
and shape-pin contract checks (five tabs, bridge rails, LimeZu credit, and every
tool this widget's JS is coded to call)."""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

from engram_server.app_widget import (
    APP_HTML,
    APP_MIME,
    APP_URI,
    app_launcher_meta,
    app_only_meta,
    app_resource_meta,
    build_app_html,
    register_app_widget,
)
from engram_server.config import Settings

MAX_HTML_CHARS = 125_000

# Every tool the widget's JS is coded against (per the build brief) — pinned so a
# future edit can't silently drop a callTool("...") without the test catching it.
CALLED_TOOLS = [
    "kb_projects",
    "kb_load",
    "kb_search",
    "kb_read",
    "kb_artifacts",
    "kb_inbox",
    "kb_common_ground",
    "social_state",
    "social_conversation",
    "social_send",
    "social_accept",
    "social_mark_read",
    "explore_state",
    "explore_profile",
    "explore_concept",
    "explore_follow",
    "explore_ask",
    "office_state",
    "team_state",
    "rooms_state",
    "room_transcript",
    "room_reply",
]

TAB_LABELS = ["Home", "Browse", "People", "Rooms", "Office"]


# --------------------------------------------------------------- meta helpers


def test_tool_meta_off_is_none() -> None:
    assert app_launcher_meta(False) is None
    assert app_only_meta(False) is None


def test_launcher_meta_on_shape() -> None:
    assert app_launcher_meta(True) == {
        "ui": {"resourceUri": APP_URI, "visibility": ["model", "app"]}
    }


def test_app_only_meta_on_shape() -> None:
    meta = app_only_meta(True)
    assert meta == {"ui": {"visibility": ["app"]}}
    assert "resourceUri" not in meta["ui"]


def test_resource_meta_shape() -> None:
    meta = app_resource_meta()
    assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert meta["ui"]["prefersBorder"] is True
    assert meta["openai/widgetPrefersBorder"] is True


def test_mime_constant() -> None:
    assert APP_MIME == "text/html;profile=mcp-app"


def test_uri_constant() -> None:
    assert APP_URI == "ui://engram/app"


# --------------------------------------------------------------- build_app_html


@pytest.fixture
def widget_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"widget": True})


def test_build_app_html_returns_str_and_stamps_explorer_url(widget_settings: Settings) -> None:
    html = build_app_html(widget_settings)
    assert isinstance(html, str)
    assert "__EXPLORER_URL__" not in html
    assert widget_settings.explorer_url.rstrip("/") in html


def test_html_size_budget() -> None:
    assert len(APP_HTML) <= MAX_HTML_CHARS


def test_html_has_all_five_tabs() -> None:
    for label in TAB_LABELS:
        assert f">{label}<" in APP_HTML or f">{label}<span" in APP_HTML, label


def test_html_has_bridge_marker() -> None:
    assert "ui/initialize" in APP_HTML
    assert "ui/notifications/tool-result" in APP_HTML
    assert "ui/resource-teardown" in APP_HTML


def test_html_has_limezu_credit() -> None:
    assert "Art: LimeZu" in APP_HTML


def test_html_calls_every_contract_tool() -> None:
    for name in CALLED_TOOLS:
        assert f'"{name}"' in APP_HTML, name


def test_html_is_self_contained_mcp_app() -> None:
    assert APP_HTML.startswith("<!DOCTYPE html>")
    assert "<script" in APP_HTML


def test_html_has_no_external_requests() -> None:
    assert 'src="http' not in APP_HTML
    assert 'href="http' not in APP_HTML
    assert "https://" not in APP_HTML
    assert "http://" not in APP_HTML


def test_html_unwraps_sdk_result_envelope() -> None:
    assert "function unwrap(" in APP_HTML
    assert "unwrap(pr.structuredContent" in APP_HTML


def test_html_jsonrpc_and_appinfo_rails() -> None:
    assert 'jsonrpc!=="2.0"' in APP_HTML
    assert "appInfo" in APP_HTML
    assert "clientInfo" not in APP_HTML


def test_html_stops_polling_on_teardown() -> None:
    assert "stopAllPolling(" in APP_HTML
    assert "stopOfficeAnim(" in APP_HTML


def test_html_has_one_esc_and_one_avatar_helper() -> None:
    # single shared esc()/avatarHtml() — not one per tab
    assert APP_HTML.count("function esc(") == 1
    assert APP_HTML.count("function avatarHtml(") == 1


# --------------------------------------------------------- resource registration


def test_register_off_no_resource(settings: Settings) -> None:
    mcp = FastMCP("test-app-off")
    register_app_widget(mcp, settings)
    resources = asyncio.run(mcp.list_resources())
    assert not any(str(r.uri) == APP_URI for r in resources)


def test_register_on_resource_present_with_meta(widget_settings: Settings) -> None:
    mcp = FastMCP("test-app-on")
    register_app_widget(mcp, widget_settings)
    resources = asyncio.run(mcp.list_resources())
    match = [r for r in resources if str(r.uri) == APP_URI]
    assert len(match) == 1
    assert match[0].mimeType == APP_MIME
    assert match[0].meta == app_resource_meta()


async def test_registered_resource_serves_html(widget_settings: Settings) -> None:
    mcp = FastMCP("test-app-serve")
    register_app_widget(mcp, widget_settings)
    contents = await mcp.read_resource(APP_URI)
    body = "".join(c.content for c in contents)
    assert body.startswith("<!DOCTYPE html>")
    assert len(body) <= MAX_HTML_CHARS
