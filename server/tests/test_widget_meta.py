"""MCP Apps (ext-apps 2026-01-26 / MCP core 2026-07-28) compliance audit.

Pins the shape every widget-mounting tool and every ``ui://`` resource must carry
across BOTH widgets (Navigator + Meetings): nested resourceUri only (the flat
``_meta["ui/resourceUri"]`` form is deprecated and being removed before GA — it
must never appear), an explicit ``visibility`` array, resource CSP, and the exact
MIME string. Also guards the bridge-level rails (jsonrpc guard, appInfo, size
reporting, teardown ack, result-envelope unwrap) that both widgets' HTML share.
"""

from __future__ import annotations

import asyncio
import re

from engram_server import meetings_widget, navigator

# ------------------------------------------------------------- resource meta


def test_navigator_resource_meta_has_csp_and_border() -> None:
    meta = navigator.navigator_resource_meta()
    assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert meta["ui"]["prefersBorder"] is True


def test_meetings_resource_meta_has_csp_and_border() -> None:
    meta = meetings_widget.meetings_resource_meta()
    assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert meta["ui"]["prefersBorder"] is True


def test_both_mime_strings_are_the_exact_spec_string() -> None:
    assert navigator.NAVIGATOR_MIME == "text/html;profile=mcp-app"
    assert meetings_widget.MEETINGS_MIME == "text/html;profile=mcp-app"


# ------------------------------------------------------------- tool meta shape


def test_navigator_tool_meta_is_nested_only_with_visibility() -> None:
    meta = navigator.navigator_tool_meta(True)
    assert meta == {
        "ui": {"resourceUri": navigator.NAVIGATOR_URI, "visibility": ["model", "app"]}
    }
    # the deprecated flat key must never be emitted
    assert "ui/resourceUri" not in meta


def test_meetings_tool_meta_is_nested_only_with_visibility() -> None:
    meta = meetings_widget.meetings_tool_meta(True)
    assert meta == {
        "ui": {"resourceUri": meetings_widget.MEETINGS_URI, "visibility": ["model", "app"]}
    }
    assert "ui/resourceUri" not in meta


def test_meetings_app_tool_meta_is_app_only_and_carries_no_resource_uri() -> None:
    # app-only tools mount nothing — they're the data plane the ALREADY-mounted
    # widget calls over the bridge, so no resourceUri is needed, only visibility.
    meta = meetings_widget.meetings_app_tool_meta(True)
    assert meta == {"ui": {"visibility": ["app"]}}
    assert "resourceUri" not in meta["ui"]


def test_tool_meta_off_is_none_for_both_widgets() -> None:
    assert navigator.navigator_tool_meta(False) is None
    assert meetings_widget.meetings_tool_meta(False) is None
    assert meetings_widget.meetings_app_tool_meta(False) is None


# --------------------------------------------------------- registered tool audit


def _all_tool_meta() -> dict[str, dict | None]:
    import engram_server.app as app_module

    return {t.name: t.meta for t in asyncio.run(app_module.mcp.list_tools())}


def test_no_flat_resource_uri_key_anywhere_in_registered_tools() -> None:
    # The deprecated flat _meta["ui/resourceUri"] form is being removed before GA —
    # assert it never appears on ANY currently-registered tool, widget on or off.
    for name, meta in _all_tool_meta().items():
        if meta is None:
            continue
        assert "ui/resourceUri" not in meta, name


def test_every_widget_meta_that_carries_ui_has_explicit_visibility() -> None:
    # Any tool wired to a ui:// resource must carry an explicit visibility array —
    # never rely on an implicit host default now that the field is standard.
    for name, meta in _all_tool_meta().items():
        if meta is None or "ui" not in meta:
            continue
        assert "visibility" in meta["ui"], name
        assert isinstance(meta["ui"]["visibility"], list) and meta["ui"]["visibility"], name


# ------------------------------------------------------ shared bridge HTML rails
#
# Both widgets are independent self-contained HTML documents (no shared JS file —
# the established one-file-per-widget pattern), so each must carry its own copy of
# every rail below rather than importing a common module.

_HTML_BY_WIDGET = {
    "navigator": navigator.NAVIGATOR_HTML,
    "meetings": meetings_widget.MEETINGS_HTML,
}


def test_jsonrpc_guard_present_in_every_widget() -> None:
    for name, html in _HTML_BY_WIDGET.items():
        assert 'jsonrpc!=="2.0"' in html, name
        assert 'jsonrpc:"2.0"' in html, name


def test_appinfo_not_clientinfo_in_every_widget() -> None:
    for name, html in _HTML_BY_WIDGET.items():
        assert "appInfo" in html, name
        assert "clientInfo" not in html, name


def test_size_changed_notification_in_every_widget() -> None:
    for name, html in _HTML_BY_WIDGET.items():
        assert "ui/notifications/size-changed" in html, name


def test_teardown_is_acknowledged_in_every_widget() -> None:
    for name, html in _HTML_BY_WIDGET.items():
        assert "ui/resource-teardown" in html, name
        # the ack echoes the request id back with an (empty) result — a bare
        # notification with no id would never satisfy a host awaiting a reply.
        assert re.search(r"id:m\.id\s*,\s*result:\{\}", html), name


def test_result_envelope_unwrap_in_every_widget() -> None:
    # field note #1 (brain-navigator.md): the python SDK wraps list/dict tool
    # returns in a single-key {"result": ...} envelope — every widget must unwrap
    # it everywhere or every shape check silently fails on claude.ai.
    for name, html in _HTML_BY_WIDGET.items():
        assert "function unwrap(" in html, name


def test_initialize_handshake_sequence_in_every_widget() -> None:
    for name, html in _HTML_BY_WIDGET.items():
        assert '"ui/initialize"' in html, name
        assert "ui/notifications/initialized" in html, name


def test_no_external_requests_in_meetings_widget() -> None:
    # mirrors the navigator's no-external-requests guard for the new widget
    html = meetings_widget.MEETINGS_HTML
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "https://" not in html
    assert "http://" not in html


def test_meetings_html_is_self_contained_mcp_app() -> None:
    assert meetings_widget.MEETINGS_HTML.startswith("<!DOCTYPE html>")
    assert "<script" in meetings_widget.MEETINGS_HTML
