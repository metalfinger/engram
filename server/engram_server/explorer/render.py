"""Frontmatter splitting, markdown rendering, and internal-link rewriting."""

from __future__ import annotations

import posixpath
import re

import markdown
import yaml

_HREF_RE = re.compile(r'href="([^"]*)"')


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
    return rewrite_links(html, posixpath.dirname(current_path))


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
