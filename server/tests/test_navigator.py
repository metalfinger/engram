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
    get_navigator_html,
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


# ------------------------------------------- full-suite features (search/inbox/basket)


def test_html_all_tabs_present_and_enabled() -> None:
    # M2 enabled the Search and Inbox tabs — no `disabled`, no leftover "M2" pills.
    for tab in ("home", "browse", "search", "inbox"):
        assert f'data-tab="{tab}"' in NAVIGATOR_HTML
    assert "M2" not in NAVIGATOR_HTML  # placeholders removed now the tabs work


def test_html_search_is_debounced() -> None:
    # 300ms debounce on the search input (plus Enter to fire immediately)
    assert "setTimeout(runSearch, 300)" in NAVIGATOR_HTML
    assert "300ms debounce" in NAVIGATOR_HTML
    assert 'callTool("kb_search"' in NAVIGATOR_HTML


def test_html_ui_message_is_array_of_blocks() -> None:
    # CRITICAL: ui/message content MUST be an ARRAY of blocks — a single object
    # silently fails to wake the agent. Guard the exact shape.
    assert 'content:[{type:"text",text:text}]' in NAVIGATOR_HTML
    assert '"ui/message"' in NAVIGATOR_HTML


def test_html_inbox_archives_via_kb_mark_read() -> None:
    assert 'callTool("kb_mark_read",{message_path:path})' in NAVIGATOR_HTML
    # act-on-message handoff carries the message path + title
    assert "Act on this inter-session message from the brain:" in NAVIGATOR_HTML


def test_html_inbox_fans_out_only_unread_projects() -> None:
    # inbox aggregates: kb_projects, then kb_load ONLY for projects with unread > 0
    assert 'callTool("kb_projects"' in NAVIGATOR_HTML
    assert "(p.unread_messages|0)>0" in NAVIGATOR_HTML


def test_html_basket_persists_in_widget_state() -> None:
    # widgetState carries the basket (selection survives re-mount)
    assert "basket:state.basket" in NAVIGATOR_HTML
    assert "setWidgetState" in NAVIGATOR_HTML
    # and boot restores it
    assert "Array.isArray(st.basket)" in NAVIGATOR_HTML


def test_html_basket_build_message_shape() -> None:
    # BUILD demands a real Artifact with per-type craft direction, from the ordered paths
    assert "kb_read each path first" in NAVIGATOR_HTML
    assert "proper ARTIFACT" in NAVIGATOR_HTML
    assert "STATUS REPORT" in NAVIGATOR_HTML
    assert "Cite the source paths" in NAVIGATOR_HTML
    # all six artifact types incl. Custom
    for label in (
        "Status report",
        "Spec document",
        "Blog post",
        "Summary",
        "Handoff brief",
        "Custom",
    ):
        assert label in NAVIGATOR_HTML


def test_html_artifacts_tab_and_listing() -> None:
    # the Artifacts tab exists and lists via kb_artifacts
    assert 'data-tab="artifacts"' in NAVIGATOR_HTML
    assert 'callTool("kb_artifacts"' in NAVIGATOR_HTML
    # empty state points back to the basket workflow
    assert "No artifacts yet — build one from the basket." in NAVIGATOR_HTML


def test_html_artifacts_share_unshare_and_url_box() -> None:
    # Share / Unshare drive the revocable-link tools
    assert 'callTool("kb_share_artifact"' in NAVIGATOR_HTML
    assert 'callTool("kb_unshare_artifact"' in NAVIGATOR_HTML
    # the minted URL is shown in a one-click-selectable codebox
    assert "share-url" in NAVIGATOR_HTML
    # a stale artifact is flagged in the row
    assert "sources changed" in NAVIGATOR_HTML


def test_html_basket_offers_to_save_as_artifact() -> None:
    # the build brief now closes with an offer to persist the result as an artifact
    assert "OFFER to save it into the brain" in NAVIGATOR_HTML
    assert "type: artifact" in NAVIGATOR_HTML
    assert "Artifacts tab" in NAVIGATOR_HTML


def test_html_has_polish_affordances() -> None:
    # loading skeletons, retry-able errors, toasts, tab badge, reconcile-on-boot
    assert "skel" in NAVIGATOR_HTML
    assert "data-retry" in NAVIGATOR_HTML
    assert "showToast" in NAVIGATOR_HTML
    assert "tbadge" in NAVIGATOR_HTML


# ---------------------------------------------------- app.py wiring (flag on/off)
#
# app.py reads settings once at import (lru_cache), so exercising the flag means
# reloading the module with ENGRAM_WIDGET set. We reload back to the default at
# the end so no other test inherits a flag-on app module.


def _reload_app(monkeypatch, *, widget: bool):
    from engram_server import config

    # Pin explicitly either way: deleting the env var would fall through to the
    # repo's .env file (which enables the widget in production).
    monkeypatch.setenv("ENGRAM_WIDGET", "1" if widget else "0")
    config.get_settings.cache_clear()
    import engram_server.app as app_module

    return importlib.reload(app_module)


def test_app_flag_off_tools_have_no_meta(monkeypatch) -> None:
    app_module = _reload_app(monkeypatch, widget=False)
    try:
        tools = {t.name: t for t in asyncio.run(app_module.mcp.list_tools())}
        for name in ("kb_projects", "kb_load", "kb_search", "kb_artifacts"):
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
        for name in ("kb_projects", "kb_load", "kb_search", "kb_artifacts"):
            assert tools[name].meta == wired, name
        # every OTHER kb_* tool stays plain
        for name in (
            "kb_read",
            "kb_write",
            "kb_append_log",
            "kb_leave_message",
            "kb_mark_read",
            "kb_rename_project",
            "kb_share_artifact",
            "kb_unshare_artifact",
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


# ------------------------------------------- recipes / quick-capture / graph link


def test_html_save_as_recipe_message_shape() -> None:
    # a second basket-footer button that saves the ordered sources + instruction
    assert "Save as recipe" in NAVIGATOR_HTML
    assert "function saveRecipe(" in NAVIGATOR_HTML
    # the ui/message tells the agent to kb_write a recipe concept
    assert "Save this as a reusable recipe" in NAVIGATOR_HTML
    assert "type: recipe" in NAVIGATOR_HTML
    assert "sources: [the ordered paths below]" in NAVIGATOR_HTML
    assert "rebuild_artifact" in NAVIGATOR_HTML
    # project derives from the basket paths (projects/<project>/…)
    assert "function dominantProject(" in NAVIGATOR_HTML


def test_html_run_recipe_message_shape() -> None:
    # recipes list via the real kb_recipes tool; a per-row Run rebuilds
    assert 'callTool("kb_recipes"' in NAVIGATOR_HTML
    assert "data-run-recipe" in NAVIGATOR_HTML
    assert "Run the recipe " in NAVIGATOR_HTML
    assert "build per its instruction as a proper ARTIFACT" in NAVIGATOR_HTML


def test_html_recipes_use_kb_recipes_not_search_hack() -> None:
    # CHANGE 1: recipes come from the dedicated kb_recipes tool; the old
    # kb_search(query:"recipe", type:"recipe") beta hack is gone entirely.
    assert 'callTool("kb_recipes",{})' in NAVIGATOR_HTML
    assert 'query:"recipe", type:"recipe"' not in NAVIGATOR_HTML
    # the "beta" chip on the recipes section is removed (real tool now)
    assert 'class="chip beta"' not in NAVIGATOR_HTML
    assert "chip beta" not in NAVIGATOR_HTML


def test_html_recipe_rows_show_instruction_and_source_count() -> None:
    # kb_recipes carries real `instruction` + `sources`, so rows surface them
    assert "r.instruction" in NAVIGATOR_HTML
    assert "r.sources" in NAVIGATOR_HTML
    assert 'class="artmeta"' in NAVIGATOR_HTML  # truncated instruction line
    assert '<span class="k">src</span>' in NAVIGATOR_HTML  # source-count chip


def test_html_artifacts_filter_row() -> None:
    # [ All | Artifacts | Recipes ] filter (buttons built from this key list)
    assert 'class="filterrow"' in NAVIGATOR_HTML
    assert 'data-filter="' in NAVIGATOR_HTML
    assert '["all","artifacts","recipes"]' in NAVIGATOR_HTML
    assert "function setFilter(" in NAVIGATOR_HTML


def test_html_artifacts_and_recipes_group_by_project() -> None:
    # CHANGE 2: rows group under a per-project section-label sub-header (id + count)
    # when >1 project is present, and fall back to a flat list at <=1 project. The
    # same groupByProject helper renders both the Artifacts and Recipes sections.
    assert "function groupByProject(" in NAVIGATOR_HTML
    # flat-list guard when a single (or no) project owns the rows
    assert "if(order.length<=1) return rows.map(rowFn).join" in NAVIGATOR_HTML
    # project sub-header carries the project id and a count
    assert 'class="section-label pgroup"' in NAVIGATOR_HTML
    assert 'class="pgcount"' in NAVIGATOR_HTML
    assert "byProj[pj].length" in NAVIGATOR_HTML
    # both sections route through the grouping helper
    assert "groupByProject(rows, artifactRow)" in NAVIGATOR_HTML
    assert "groupByProject(rows, recipeRow)" in NAVIGATOR_HTML


def test_html_quick_capture_uses_kb_inbox_with_fallback() -> None:
    # top-of-Home capture row -> kb_inbox, success + graceful not-live toasts
    assert 'callTool("kb_inbox",{text:text})' in NAVIGATOR_HTML
    assert "Quick capture — remember anything…" in NAVIGATOR_HTML
    assert "Captured to inbox" in NAVIGATOR_HTML
    assert "Inbox tool not live yet" in NAVIGATOR_HTML


def test_html_graph_link_uses_sentinel() -> None:
    # the constant carries the sentinel (never a literal host — no-external test)
    assert 'href="__EXPLORER_URL__/brain/graph"' in NAVIGATOR_HTML
    assert 'class="graphlink"' in NAVIGATOR_HTML


def test_get_navigator_html_replaces_sentinel() -> None:
    # render function stamps the explorer host in; served HTML has no sentinel
    assert "__EXPLORER_URL__" in NAVIGATOR_HTML
    html = get_navigator_html("https://brain.example.com/")
    assert "__EXPLORER_URL__" not in html
    # trailing slash trimmed; /brain/graph appended; safe target + rel intact
    assert 'href="https://brain.example.com/brain/graph"' in html
    assert 'target="_blank" rel="noopener"' in html


def test_register_on_serves_html_without_sentinel() -> None:
    # the registered resource serves rendered HTML (sentinel already replaced)
    mcp = FastMCP("test-render")
    register_navigator(mcp, True)
    contents = asyncio.run(mcp.read_resource(NAVIGATOR_URI))
    body = "".join(c.content for c in contents)
    assert "__EXPLORER_URL__" not in body
    assert "/brain/graph" in body


def test_html_unwraps_sdk_result_envelope():
    """The python SDK wraps list/dict tool returns in {"result": ...} — the widget
    must unwrap that envelope everywhere or every shape check fails on claude.ai."""
    from engram_server.navigator import NAVIGATOR_HTML

    assert "function unwrap(" in NAVIGATOR_HTML
    assert 'unwrap(pr.structuredContent' in NAVIGATOR_HTML
    assert "unwrap(r.structuredContent)" in NAVIGATOR_HTML


def test_html_artifacts_open_rendered_not_source() -> None:
    """HTML artifacts are experiences: the reader links to the rendered share page
    and collapses source; rows get a View link; never renderMarkdown of raw HTML."""
    from engram_server.navigator import NAVIGATOR_HTML, get_navigator_html

    assert "function readerBody" in NAVIGATOR_HTML
    assert 'String(meta.format||"")!=="html"' in NAVIGATOR_HTML
    assert "Open rendered page ↗" in NAVIGATOR_HTML
    assert "data-view-url" in NAVIGATOR_HTML  # artifacts-row open w/ popup fallback
    assert "function viewArtifact" in NAVIGATOR_HTML
    assert "Link sent to chat" in NAVIGATOR_HTML
    rendered = get_navigator_html("https://brain.example")
    assert "__EXPLORER_URL__" not in rendered
    assert "https://brain.example/share/" in rendered.replace("'+esc(meta.share)+'", "TOK") or "https://brain.example/share/" in rendered


def test_html_reader_shows_supersession_chips() -> None:
    # the reader surfaces supersession from frontmatter (superseded_by / supersedes /
    # valid_until), rendered defensively and linking via the in-widget open flow.
    assert "function supersedeChips(" in NAVIGATOR_HTML
    assert "function supersedeNote(" in NAVIGATOR_HTML
    assert "superseded by → " in NAVIGATOR_HTML
    assert "supersedes " in NAVIGATOR_HTML
    # chips + note links navigate in-widget (data-open-path), never a raw external link
    assert 'class="chip supersede" data-open-path=' in NAVIGATOR_HTML
    assert 'class="snl" data-open-path=' in NAVIGATOR_HTML
    # single-path OR list frontmatter both handled
    assert "function asPaths(" in NAVIGATOR_HTML
    # superseded concepts read as historical: muted, struck title
    assert "rhead.superseded" in NAVIGATOR_HTML
    # valid_until surfaces as its own chip
    assert '<span class="k">until</span>' in NAVIGATOR_HTML


def test_every_external_open_has_popup_fallback() -> None:
    """No dead buttons: reader Open-rendered is a data-view-url button, and a
    blanket interceptor routes ALL target=_blank anchors through viewArtifact."""
    from engram_server.navigator import NAVIGATOR_HTML

    assert "Open rendered page ↗</button>" in NAVIGATOR_HTML  # button, not bare anchor
    assert "a[target=\"_blank\"]" in NAVIGATOR_HTML.replace("'", '"') or 'a[target="_blank"]' in NAVIGATOR_HTML
    assert "viewArtifact(xa.href)" in NAVIGATOR_HTML
