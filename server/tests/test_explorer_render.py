"""Unit tests for explorer render helpers, formatters, and HTML templates."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from engram_server.explorer.format import (
    humanize_time,
    is_expired,
    properties_panel,
    score_bar,
    stamp,
    type_glyph,
)
from engram_server.explorer.html import badge, button, card, chip, codebox, page
from engram_server.explorer.render import render_markdown, rewrite_links, split_frontmatter
from engram_server.explorer.routes import (
    _backlinks,
    _concept_card,
    _concept_footer,
    _concept_month,
    _count_concepts,
    _count_sessions,
    _crumbs_for,
    _result_card,
    _setup_body,
    _sidebar,
    _siblings,
    _timeline,
)
from engram_server.explorer.setup import render_setup_script

SKELETON = Path(__file__).resolve().parents[2] / "brain-skeleton" / "brain"
_NOW = dt.datetime(2026, 7, 4, 12, 0, tzinfo=dt.timezone.utc)

# ------------------------------------------------------------ rewrite_links


def test_rewrite_sibling_link() -> None:
    html = '<a href="notes.md">notes</a>'
    out = rewrite_links(html, "projects/alt")
    assert 'href="/brain/f/projects/alt/notes.md"' in out


def test_rewrite_cross_project_link() -> None:
    html = '<a href="../hyprlocl/context.md">ctx</a>'
    out = rewrite_links(html, "projects/alt")
    assert 'href="/brain/f/projects/hyprlocl/context.md"' in out


def test_absolute_http_and_mailto_untouched() -> None:
    html = (
        '<a href="https://example.com/x.md">x</a>'
        '<a href="http://a.b/">y</a>'
        '<a href="mailto:h@x.y">m</a>'
    )
    assert rewrite_links(html, "projects/alt") == html


def test_fragment_only_untouched() -> None:
    html = '<a href="#open-loops">frag</a>'
    assert rewrite_links(html, "projects/alt") == html


def test_fragment_preserved_on_rewrite() -> None:
    out = rewrite_links('<a href="spec.md#phase-2">spec</a>', "projects/alt")
    assert 'href="/brain/f/projects/alt/spec.md#phase-2"' in out


def test_escape_attempt_not_linked() -> None:
    out = rewrite_links('<a href="../../..">bad</a>', "projects/alt")
    assert "/brain/f" not in out
    assert "bad" in out  # anchor text survives, just unlinked


def test_deep_escape_attempt_not_linked() -> None:
    out = rewrite_links('<a href="../../../etc/passwd">bad</a>', "projects/alt")
    assert "/brain/f" not in out
    assert "etc/passwd" not in out


def test_render_markdown_rewrites_relative_links() -> None:
    out = render_markdown("See [spec](specs/api.md).", "projects/alt/context.md")
    assert 'href="/brain/f/projects/alt/specs/api.md"' in out


# ------------------------------------------------------------ split_frontmatter


def test_split_frontmatter_absent() -> None:
    text = "# Just a heading\n\nBody text."
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_broken_yaml() -> None:
    text = "---\n: not: [valid yaml\n---\nbody"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_unterminated() -> None:
    text = "---\ntype: message\nno closing fence"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_valid() -> None:
    text = "---\ntype: message\nstatus: unread\n---\nhello"
    meta, body = split_frontmatter(text)
    assert meta == {"type": "message", "status": "unread"}
    assert body.strip() == "hello"


# ------------------------------------------------------------ html templates


def test_page_contains_nav_and_escaped_title() -> None:
    doc = page("<Tools> & things", "<p>hi</p>", [("Brain", "/brain")])
    assert 'href="/brain"' in doc
    assert 'href="/brain/system"' in doc
    assert 'href="/brain/activity"' in doc
    assert "&lt;Tools&gt; &amp; things" in doc
    assert "<p>hi</p>" in doc
    assert "<script" not in doc.lower()  # zero JavaScript by design


def test_badge_and_card_escape_content() -> None:
    b = badge("<unread>", "unread")
    assert "&lt;unread&gt;" in b
    assert 'class="badge unread"' in b
    c = card("/brain/p/alt", 'Alt "Inc"', "desc & more", b)
    assert "&amp; more" in c
    assert "&lt;unread&gt;" in c


def test_chip_link_and_plain_escape() -> None:
    assert 'class="chip tag"' in chip("infra", cls="tag")
    linked = chip("alt", "/brain/p/alt")
    assert 'href="/brain/p/alt"' in linked
    assert "&amp;" in chip("a & b")


def test_page_carries_sidebar_and_search_value() -> None:
    doc = page("Title", "<p>x</p>", [("brain", "/brain")], sidebar_html="<nav>SIDE</nav>", search_value="dns")
    assert "SIDE" in doc
    assert 'value="dns"' in doc
    assert 'action="/brain/search"' in doc


# ------------------------------------------------------------ task lists


def test_task_list_renders_checkboxes() -> None:
    out = render_markdown("- [ ] todo\n- [x] done", "self/x.md")
    assert 'type="checkbox"' in out
    assert "checked" in out
    assert 'class="task"' in out


# ------------------------------------------------------------ type glyphs / stamp


def test_type_glyph_known_and_default() -> None:
    assert type_glyph("decision") == "⚖"
    assert type_glyph("PROJECT") == "📁"
    assert type_glyph(None) == "◆"
    assert type_glyph("unheard-of") == "◆"


def test_stamp_wraps_glyph() -> None:
    assert "📐" in stamp("spec")
    assert 'class="stamp-sm"' in stamp("spec", small=True)


# ------------------------------------------------------------ humanize_time


def test_humanize_days_ago() -> None:
    rel, exact = humanize_time("2026-07-02", _NOW)
    assert rel == "2 days ago"
    assert exact == "2026-07-02"


def test_humanize_just_now() -> None:
    rel, _ = humanize_time("2026-07-04T12:00:00Z", _NOW)
    assert rel == "just now"


def test_humanize_future() -> None:
    rel, _ = humanize_time("2026-07-06", _NOW)
    assert rel.startswith("in ") and "day" in rel


def test_humanize_months() -> None:
    rel, _ = humanize_time("2026-04-04", _NOW)
    assert rel == "3 months ago"


def test_humanize_unparseable_roundtrips() -> None:
    rel, exact = humanize_time("not a date", _NOW)
    assert rel == exact == "not a date"


# ------------------------------------------------------------ is_expired


def test_is_expired_boundaries() -> None:
    today = dt.date(2026, 7, 4)
    assert is_expired("2026-07-01", today) is True
    assert is_expired("2026-07-04", today) is False  # strict: today is not expired
    assert is_expired("2026-08-01", today) is False
    assert is_expired("garbage", today) is False
    assert is_expired(None, today) is False


# ------------------------------------------------------------ properties_panel


def test_properties_panel_rows_and_dot() -> None:
    meta = {
        "type": "decision",
        "status": "active",
        "project": "alt",
        "tags": ["infra", "dns"],
        "timestamp": "2026-07-02",
    }
    html = properties_panel(meta, dt.date(2026, 7, 4), now=_NOW)
    assert "Type" in html and "decision" in html
    assert 'class="dot active"' in html
    assert 'href="/brain/p/alt"' in html
    assert 'class="chip tag"' in html
    assert "2 days ago" in html


def test_properties_panel_expired_message() -> None:
    meta = {"type": "message", "to": "claude-code", "priority": "high", "expires": "2026-07-01"}
    html = properties_panel(meta, dt.date(2026, 7, 4))
    assert "expired" in html
    assert "priority-high" in html


def test_properties_panel_empty_is_blank() -> None:
    assert properties_panel({}, dt.date(2026, 7, 4)) == ""


# ------------------------------------------------------------ score_bar


def test_score_bar_clamps_and_labels() -> None:
    assert "width:75%" in score_bar(0.75)
    assert "75%" in score_bar(0.75)
    assert "width:100%" in score_bar(1.5)
    assert "width:0%" in score_bar(-0.2)


# ------------------------------------------------------------ search result card


def test_result_card_path_heading_score() -> None:
    r = {
        "path": "projects/alt/decisions/x.md",
        "title": "Search engine choice",
        "description": "why we picked ripgrep",
        "score": 0.5,
        "matched_heading": "Phase 2",
    }
    html = _result_card(r)
    assert 'href="/brain/f/projects/alt/decisions/x.md"' in html
    assert "projects" in html and "decisions" in html
    assert "›" in html  # path breadcrumb separator
    assert "Phase 2" in html
    assert "50%" in html


# ------------------------------------------------------------ sidebar


def test_sidebar_lists_sections_and_projects() -> None:
    html = _sidebar(SKELETON, "/brain/p/alt")
    assert "Projects" in html and "Self" in html and "Library" in html
    assert "nav-proj" in html
    assert 'href="/brain/p/alt"' in html


def test_sidebar_marks_active_file() -> None:
    html = _sidebar(SKELETON, "/brain/f/self/stack.md")
    assert 'class="nav-link active" href="/brain/f/self/stack.md"' in html


def test_sidebar_opens_active_project_subtree() -> None:
    html = _sidebar(SKELETON, "/brain/f/projects/alt/context.md")
    # the alt subtree must be <details open> because the active file lives inside it
    assert "<details class=\"nav-proj\" open>" in html


def test_sidebar_has_setup_link() -> None:
    html = _sidebar(SKELETON, "/brain/setup")
    assert 'class="nav-link active" href="/brain/setup"' in html


# ------------------------------------------------------------ log timeline (both formats)


def test_timeline_old_format_dash_heading() -> None:
    entries = ["## 2026-07-04 — Bundle initialized\n\nSkeleton created from the handoff."]
    out = _timeline(entries, "projects/alt/log.md")
    assert 'class="tl-date">2026-07-04</span>' in out
    assert "<h3>Bundle initialized</h3>" in out
    assert "Skeleton created" in out


def test_timeline_new_format_bare_date_bullets() -> None:
    entries = [
        "## 2026-07-04\n"
        "* **Bundle initialized** — skeleton created.\n"
        "* **Search picked** — went with ripgrep."
    ]
    out = _timeline(entries, "projects/alt/log.md")
    assert 'class="tl-date">2026-07-04</span>' in out
    # the bare date must NOT be duplicated as a title heading
    assert "<h3>2026-07-04</h3>" not in out
    # bullets render, bold entry titles preserved
    assert "Bundle initialized" in out and "Search picked" in out
    assert "<strong>" in out and "<li" in out


# ------------------------------------------------------------ depth helpers (mini bundle)


def _mini_bundle(tmp_path: Path) -> Path:
    brain = tmp_path / "brain"
    alt = brain / "projects" / "alt"
    dec = alt / "decisions"
    dec.mkdir(parents=True)
    (dec / "index.md").write_text("# Decisions\n\n* things\n", encoding="utf-8")
    (dec / "2026-07-search.md").write_text(
        "---\ntype: decision\ntitle: Search engine choice\nconfidence: settled\n"
        "timestamp: 2026-07-02T00:00:00Z\n---\n# Search\nPicked ripgrep.\n",
        encoding="utf-8",
    )
    (dec / "2026-06-hosting.md").write_text(
        "---\ntype: decision\ntitle: Hosting choice\nconfidence: tentative\n---\n"
        "# Hosting\nRelated: [search](2026-07-search.md).\n",
        encoding="utf-8",
    )
    (alt / "context.md").write_text(
        "---\ntype: project\ntitle: alt\n---\n# About\n"
        "Decided via [the search choice](decisions/2026-07-search.md).\n",
        encoding="utf-8",
    )
    (alt / "log.md").write_text(
        "# Log — alt\n\n## 2026-07-04\n* **x** — y\n\n## 2026-07-03 — Older\n\nbody\n",
        encoding="utf-8",
    )
    return brain


def test_count_concepts_excludes_index(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    assert _count_concepts(brain / "projects" / "alt" / "decisions") == 2
    assert _count_concepts(brain / "projects" / "alt" / "nope") == 0


def test_count_sessions_counts_both_heading_shapes(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    assert _count_sessions(brain / "projects" / "alt") == 2


def test_concept_month_prefers_filename_then_timestamp() -> None:
    assert _concept_month(Path("2026-07-search.md"), {}) == "2026-07"
    assert _concept_month(Path("bio.md"), {"timestamp": "2026-03-04T00:00:00Z"}) == "2026-03"
    assert _concept_month(Path("bio.md"), {}) == ""


def test_concept_card_shows_confidence_and_month(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    f = brain / "projects" / "alt" / "decisions" / "2026-07-search.md"
    html = _concept_card(f, brain)
    assert "Search engine choice" in html  # frontmatter title, not filename
    assert 'class="badge settled"' in html  # confidence colored
    assert "2026-07" in html  # month from YYYY-MM filename prefix
    assert 'title="projects/alt/decisions/2026-07-search.md"' in html  # path kept for copy


def test_siblings_sorted_excludes_index(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    target = brain / "projects" / "alt" / "decisions" / "2026-07-search.md"
    names = [f.name for f in _siblings(target)]
    assert names == ["2026-06-hosting.md", "2026-07-search.md"]


def test_backlinks_finds_referrers_by_title(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    bl = _backlinks(brain, "projects/alt/decisions/2026-07-search.md")
    paths = {p for p, _ in bl}
    titles = {t for _, t in bl}
    # both the sibling decision and the project context link here
    assert "projects/alt/decisions/2026-06-hosting.md" in paths
    assert "projects/alt/context.md" in paths
    # navigation noise and self-links excluded
    assert "projects/alt/decisions/index.md" not in paths
    assert "projects/alt/decisions/2026-07-search.md" not in paths
    # referrers surfaced by human title
    assert "Hosting choice" in titles and "alt" in titles


def test_concept_footer_kills_dead_ends(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    target = brain / "projects" / "alt" / "decisions" / "2026-07-search.md"
    rel = "projects/alt/decisions/2026-07-search.md"
    html = _concept_footer(target, rel, brain)
    assert "siblingnav" in html  # prev/next sibling nav
    assert "More in Decisions" in html  # folder title from index.md H1
    assert "Referenced by" in html  # backlinks panel
    assert "Hosting choice" in html  # sibling/backlink title, not a slug
    assert 'class="pathline"' in html
    assert f"<code>{rel}</code>" in html  # raw path kept visible + copyable


def test_backlinks_empty_when_nothing_links(tmp_path: Path) -> None:
    brain = _mini_bundle(tmp_path)
    html = _concept_footer(
        brain / "projects" / "alt" / "context.md", "projects/alt/context.md", brain
    )
    assert "Nothing links here yet." in html  # context.md is linked-from-nothing here


# ------------------------------------------------------------ title-based chrome


def test_crumbs_leaf_uses_title_keeps_path_href() -> None:
    crumbs = _crumbs_for("projects/alt/decisions/2026-07-search.md", "Search engine choice")
    labels = [lbl for lbl, _ in crumbs]
    assert labels[0] == "brain"
    assert labels[-1] == "Search engine choice"  # leaf humanized
    assert "2026-07-search.md" not in labels  # slug filename gone from chrome
    assert "decisions" in labels  # intermediate folder slugs stay
    assert crumbs[-1][1] == "/brain/f/projects/alt/decisions/2026-07-search.md"


def test_crumbs_without_title_falls_back_to_slug() -> None:
    crumbs = _crumbs_for("projects/alt/decisions/2026-07-search.md")
    assert crumbs[-1][0] == "2026-07-search.md"


# ------------------------------------------------------------ setup / onboarding


def test_button_and_codebox() -> None:
    b = button("Open", "https://claude.ai/settings/connectors", new_tab=True)
    assert 'class="btn"' in b
    assert 'target="_blank"' in b and 'rel="noopener"' in b
    box = codebox("claude mcp add engram")
    assert 'class="codebox"' in box
    assert "claude mcp add engram" in box
    assert "<button" not in box  # zero-JS: no copy button, selection is CSS-only


def test_setup_body_covers_every_client() -> None:
    html = _setup_body(
        "https://engram.metalfinger.xyz/mcp",
        "https://brain.metalfinger.xyz",
        "brain.metalfinger.xyz",
        "metalfinger",
        ["kb_projects", "kb_search"],
        ["hir.012612@gmail.com", "hiren@metalfinger.xyz"],
    )
    # Claude surfaces
    assert (
        "claude mcp add --transport http --scope user engram https://engram.metalfinger.xyz/mcp"
        in html
    )
    assert "/brain/setup/engram-setup.ps1" in html
    assert "claude.ai/settings/connectors" in html
    # ChatGPT + the discovery endpoint it needs
    assert "ChatGPT" in html
    assert "Developer mode" in html
    assert "/.well-known/oauth-protected-resource" in html
    # Codex CLI: command + config.toml fallback
    assert "codex mcp add engram https://engram.metalfinger.xyz/mcp" in html
    assert "[mcp_servers.engram]" in html
    # any-MCP compact card
    assert "Any MCP client" in html
    assert "Cursor" in html and "Gemini CLI" in html
    assert 'class="setup-card compact"' in html
    # browse card lists both allowed emails
    assert "hir.012612@gmail.com" in html and "hiren@metalfinger.xyz" in html
    assert "brain.metalfinger.xyz" in html
    # tool names render as chips linking to /brain/system
    assert 'href="/brain/system"' in html
    assert "kb_projects" in html
    # zero JavaScript
    assert "<script" not in html.lower()


def test_setup_script_fills_tokens_and_is_idempotent() -> None:
    script = render_setup_script("https://engram.metalfinger.xyz/mcp")
    # tokens fully substituted
    assert "__MCP_URL__" not in script and "__REPO__" not in script
    # the live MCP URL and the correct repo made it in
    assert "https://engram.metalfinger.xyz/mcp" in script
    assert "$Repo     = 'metalfinger/brain'" in script
    assert "repos/$Repo/contents/skills/engram/SKILL.md" in script
    # idempotency + skill install + graceful gh-missing handling
    assert "already registered" in script
    assert "claude mcp add --transport http --scope user engram" in script
    assert "New-Item -ItemType Directory -Force -Path $SkillDir" in script
    assert "application/vnd.github.raw" in script
    assert "gh auth login" in script
