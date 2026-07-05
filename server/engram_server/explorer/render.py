"""Frontmatter splitting, markdown rendering, and internal-link rewriting."""

from __future__ import annotations

import posixpath
import re

import markdown
import yaml

_HREF_RE = re.compile(r'href="([^"]*)"')
_TASK_RE = re.compile(r"<li>(\s*<p>)?\s*\[([ xX])\]\s")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from a concept file into (metadata, body).

    Tolerant: returns ({}, text) when frontmatter is absent, unterminated,
    unparseable, or not a mapping — the original text is never lost.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    for close in range(1, len(lines)):
        if lines[close].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:close]))
            except yaml.YAMLError:
                return {}, text
            if not isinstance(meta, dict):
                return {}, text
            return meta, "\n".join(lines[close + 1 :])
    return {}, text


def render_markdown(body: str, current_path: str) -> str:
    """Render an OKF markdown body to HTML with internal links rewritten.

    ``current_path`` is the repo-relative POSIX path of the file being
    rendered; relative links resolve against its directory.
    """
    html = markdown.markdown(body, extensions=["extra", "sane_lists"])
    html = _render_tasklists(html)
    return rewrite_links(html, posixpath.dirname(current_path))


def render_markdown_public(body: str) -> str:
    """Render an OKF markdown body for the PUBLIC share page.

    Unlike ``render_markdown``, every relative/internal link is neutralized to plain
    text — the share page must never leak repo paths as ``/brain/f/...`` hrefs. Only
    external links (http/https/mailto) survive.
    """
    html = markdown.markdown(body, extensions=["extra", "sane_lists"])
    html = _render_tasklists(html)
    return _strip_internal_hrefs(html)


def _strip_internal_hrefs(html: str) -> str:
    """Drop the href of every non-external link (anchor renders as plain text)."""

    def _rewrite(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            return match.group(0)
        return ""

    return _HREF_RE.sub(_rewrite, html)


def _render_tasklists(html: str) -> str:
    """Turn ``- [ ]`` / ``- [x]`` list items into disabled checkbox rows.

    python-markdown leaves the literal brackets; this promotes them to real
    (read-only) checkboxes so task lists read like Notion's.
    """

    def _repl(match: re.Match[str]) -> str:
        prefix = match.group(1) or ""
        checked = " checked" if match.group(2).lower() == "x" else ""
        return f'<li class="task">{prefix}<input type="checkbox" disabled{checked}> '

    return _TASK_RE.sub(_repl, html)


def rewrite_links(html: str, current_dir: str) -> str:
    """Rewrite relative href targets to /brain/f/<repo-relative-path>.

    External links (http/https/mailto) and pure #fragment anchors pass
    through untouched; #fragments on rewritten targets are preserved.
    Targets that escape the repo root lose their href entirely (the anchor
    renders as plain text).
    """

    def _rewrite(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        path, _, fragment = target.partition("#")
        joined = path.lstrip("/") if path.startswith("/") else posixpath.join(current_dir, path)
        resolved = posixpath.normpath(joined)
        if resolved.startswith(".."):
            return ""  # escapes the repo root: drop the attribute -> unlinked text
        if resolved == ".":
            resolved = ""
        suffix = f"#{fragment}" if fragment else ""
        return f'href="/brain/f/{resolved}{suffix}"'

    return _HREF_RE.sub(_rewrite, html)
