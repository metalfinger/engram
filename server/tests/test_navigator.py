"""Brain Navigator widget (SEP-1865): flag gating, meta shapes, resource
registration, tool-meta wiring, and HTML sanity.

The widget is opt-in behind ENGRAM_WIDGET. With the flag off the tools stay
plain and the ui:// resource is absent; with it on the three navigation tools
advertise the resource and the resource is registered.
"""

from __future__ import annotations

import asyncio
import importlib

from mcp.server.fastmcp import FastMCP

from engram_server import navigator
from engram_server.navigator import (
    NAVIGATOR_HTML,
    NAVIGATOR_MIME,
    NAVIGATOR_URI,
    navigator_resource_meta,
    navigator_tool_meta,
    register_navigator,
)

# --------------------------------------------------------------- meta helpers


def test_tool_meta_off_is_none() -> None:
    assert navigator_tool_meta(False) is None


def test_tool_meta_on_carries_resource_uri() -> None:
    assert navigator_tool_meta(True) == {"ui": {"resourceUri": NAVIGATOR_URI}}


def test_resource_meta_shape() -> None:
    meta = navigator_resource_meta()
    # deny-by-default CSP — the widget talks only over the bridge, no direct fetches
    assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    # bordered card chrome in BOTH the MCP-Apps and OpenAI namespaces
    assert meta["ui"]["prefersBorder"] is True
    assert meta["openai/widgetPrefersBorder"] is True


def test_mime_constant() -> None:
    assert NAVIGATOR_MIME == "text/html;profile=mcp-app"


# --------------------------------------------------------- resource registration


def test_register_off_no_resource() -> None:
    mcp = FastMCP("test-off")
    register_navigator(mcp, False)
    resources = asyncio.run(mcp.list_resources())
    assert not any(str(r.uri) == NAVIGATOR_URI for r in resources)


def test_register_on_resource_present_with_meta() -> None:
    mcp = FastMCP("test-on")
    register_navigator(mcp, True)
    resources = asyncio.run(mcp.list_resources())
    match = [r for r in resources if str(r.uri) == NAVIGATOR_URI]
    assert len(match) == 1
    res = match[0]
    assert res.mimeType == NAVIGATOR_MIME
    assert res.meta == navigator_resource_meta()


# ------------------------------------------------------------------ HTML sanity


def test_html_has_jsonrpc_guard() -> None:
    # the non-JSONRPC frame guard (claude.ai injects unrelated messages)
    assert 'jsonrpc!=="2.0"' in NAVIGATOR_HTML
    # outbound frames are proper JSON-RPC
    assert 'jsonrpc:"2.0"' in NAVIGATOR_HTML


def test_html_uses_appinfo_not_clientinfo() -> None:
    # ui/initialize params MUST use appInfo — clientInfo silently breaks tools/call
    assert "appInfo" in NAVIGATOR_HTML
    assert "clientInfo" not in NAVIGATOR_HTML


def test_html_reports_height_both_channels() -> None:
    assert "notifyIntrinsicHeight" in NAVIGATOR_HTML
    assert "ui/notifications/size-changed" in NAVIGATOR_HTML


def test_html_has_no_external_requests() -> None:
    # zero external requests: no literal http(s) src/href. The only external links
    # are built dynamically as `href="'+url+'" target="_blank"`, so no literal
    # `href="http` or `src="http` appears in the document.
    assert 'src="http' not in NAVIGATOR_HTML
    assert 'href="http' not in NAVIGATOR_HTML
    assert "https://" not in NAVIGATOR_HTML
    assert "http://" not in NAVIGATOR_HTML
    # external links, when built, carry the safe rel + target
    assert 'target="_blank" rel="noopener"' in NAVIGATOR_HTML


def test_html_is_self_contained_mcp_app() -> None:
    assert NAVIGATOR_HTML.startswith("<!DOCTYPE html>")
    assert "<script" in NAVIGATOR_HTML


# ---------------------------------------------------- app.py wiring (flag on/off)
#
# app.py reads settings once at import (lru_cache), so exercising the flag means
# reloading the module with ENGRAM_WIDGET set. We reload back to the default at
# the end so no other test inherits a flag-on app module.


def _reload_app(monkeypatch, *, widget: bool):
    from engram_server import config

    if widget:
        monkeypatch.setenv("ENGRAM_WIDGET", "1")
    else:
        monkeypatch.delenv("ENGRAM_WIDGET", raising=False)
    config.get_settings.cache_clear()
    import engram_server.app as app_module

    return importlib.reload(app_module)


def test_app_flag_off_tools_have_no_meta(monkeypatch) -> None:
    app_module = _reload_app(monkeypatch, widget=False)
    try:
        tools = {t.name: t for t in asyncio.run(app_module.mcp.list_tools())}
        for name in ("kb_projects", "kb_load", "kb_search"):
            assert tools[name].meta is None, name
        resources = asyncio.run(app_module.mcp.list_resources())
        assert not any(str(r.uri) == NAVIGATOR_URI for r in resources)
    finally:
        _reload_app(monkeypatch, widget=False)


def test_app_flag_on_wires_three_tools_and_resource(monkeypatch) -> None:
    app_module = _reload_app(monkeypatch, widget=True)
    try:
        tools = {t.name: t for t in asyncio.run(app_module.mcp.list_tools())}
        wired = {"ui": {"resourceUri": NAVIGATOR_URI}}
        for name in ("kb_projects", "kb_load", "kb_search"):
            assert tools[name].meta == wired, name
        # every OTHER kb_* tool stays plain
        for name in (
            "kb_read",
            "kb_write",
            "kb_append_log",
            "kb_leave_message",
            "kb_mark_read",
            "kb_rename_project",
        ):
            assert tools[name].meta is None, name
        resources = asyncio.run(app_module.mcp.list_resources())
        assert any(str(r.uri) == NAVIGATOR_URI for r in resources)
    finally:
        # restore the default (flag-off) app module for the rest of the suite
        _reload_app(monkeypatch, widget=False)


def test_navigator_module_reexports() -> None:
    # guard against accidental symbol renames the wiring depends on
    assert navigator.NAVIGATOR_URI == "ui://engram/navigator"
    assert callable(navigator.register_navigator)
