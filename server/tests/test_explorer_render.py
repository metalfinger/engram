"""Unit tests for explorer render helpers and HTML templates (no routes here)."""

from __future__ import annotations

from engram_server.explorer.html import badge, card, page
from engram_server.explorer.render import render_markdown, rewrite_links, split_frontmatter

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
