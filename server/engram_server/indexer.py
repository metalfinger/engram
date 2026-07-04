"""Idempotent parent-index maintenance for the brain OKF bundle.

Every concept file must be reachable from its directory's index.md; brand-new
directories are linked upward (always via explicit ``<dirname>/index.md``
targets) until the chain meets an already-indexed ancestor or the bundle root.
Index files never carry frontmatter.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

__all__ = ["ensure_indexed"]

_BULLET_MARKERS = ("* ", "- ", "+ ")


def ensure_indexed(root: Path, rel_path: str, title: str, description: str) -> list[str]:
    """Ensure the concept at ``rel_path`` is linked from its parent index.md.

    Creates missing index files and chains new directories into their parents
    as needed. Returns the POSIX rel paths of every index file modified or
    created, in modification order; ``[]`` when the concept is already linked.
    """
    rel = rel_path.replace("\\", "/").strip("/")
    parent = posixpath.dirname(rel)
    filename = posixpath.basename(rel)
    modified: list[str] = []
    _ensure_link(root, parent, filename, _bullet(title, filename, description), description, modified)
    return modified


def _ensure_link(
    root: Path, dir_rel: str, target: str, bullet: str, description: str, modified: list[str]
) -> None:
    """Ensure ``target`` is linked from ``dir_rel``'s index.md, recursing upward."""
    index_rel = posixpath.join(dir_rel, "index.md") if dir_rel else "index.md"
    index_path = root / index_rel
    if index_path.is_file():
        text = index_path.read_text(encoding="utf-8")
        if _linked(text, target):
            return
        _write(index_path, _add_bullet(text, bullet))
        modified.append(index_rel)
        return

    # Brand-new directory: create its index, then link the dir into ITS parent.
    dir_title = _dir_title(posixpath.basename(dir_rel) if dir_rel else root.name)
    _write(index_path, f"# {dir_title}\n\n{bullet}\n")
    modified.append(index_rel)
    if not dir_rel:
        return  # root reached
    dir_target = f"{posixpath.basename(dir_rel)}/index.md"
    _ensure_link(
        root,
        posixpath.dirname(dir_rel),
        dir_target,
        _bullet(dir_title, dir_target, description),
        description,
        modified,
    )


def _linked(text: str, target: str) -> bool:
    """True when a markdown link target in ``text`` ends with the bare ``target``."""
    return re.search(r"\]\((?:[^)\s]*/)?" + re.escape(target) + r"\)", text) is not None


def _add_bullet(text: str, bullet: str) -> str:
    """Insert ``bullet`` into an existing index: replace placeholder prose, else append."""
    head, body = _split_h1(text)
    if head and _is_placeholder(body):
        return "\n".join(head).rstrip() + "\n\n" + bullet + "\n"
    return text.rstrip("\n") + "\n" + bullet + "\n"


def _split_h1(text: str) -> tuple[list[str], list[str]]:
    """Split into (lines up to and including the first H1, lines below it)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return lines[: i + 1], lines[i + 1 :]
    return [], lines


def _is_placeholder(body: list[str]) -> bool:
    """True when the body is only prose placeholder lines (no headings, no bullets)."""
    stripped = [line.strip() for line in body if line.strip()]
    return all(not line.startswith("#") and not line.startswith(_BULLET_MARKERS) for line in stripped)


def _bullet(title: str, target: str, description: str) -> str:
    line = f"* [{title}]({target})"
    return f"{line} - {description}" if description else line


def _dir_title(name: str) -> str:
    """Derive a display title from a kebab-case directory name: Title Case."""
    return " ".join(part[:1].upper() + part[1:] for part in name.split("-") if part) or name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
