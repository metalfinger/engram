"""Nightly reconcile + brain-health report.

A single failure-soft pass over the whole bundle: verify frontmatter parses,
verify every concept is reachable from its parent index (repairing when not),
find dangling links, orphan concepts (no inbound body links), stale artifacts,
and DEAD KNOWLEDGE (concepts with zero inbound links that no artifact sources
either). Optionally re-embeds the whole bundle. The findings are written back as
``library/reports/brain-health.md`` (type: report, regenerated each run) so the
health of the brain is itself a first-class, browsable concept.
"""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import Any

from anyio import to_thread

from .errors import GitError
from .frontmatter import read_meta, split
from .indexer import ensure_indexed

log = logging.getLogger("engram.reconcile")

_REL_MD = re.compile(r"\]\((?!https?://|mailto:)([^)\s#]*\.md)(?:#[^)\s]*)?\)")
_REPORT_PATH = "library/reports/brain-health.md"
_REPORT_DIR = "library/reports"


def _is_index(rel: str) -> bool:
    return posixpath.basename(rel) == "index.md"


def _is_log(rel: str) -> bool:
    return posixpath.basename(rel) == "log.md"


def _is_anchor(rel: str) -> bool:
    """context.md / log.md / index.md — structural anchors, not free-standing concepts."""
    return posixpath.basename(rel) in ("context.md", "log.md", "index.md")


def _in_messages(rel: str) -> bool:
    return "messages" in rel.split("/")[:-1]


def _resolve_link(src_rel: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), target))


def _scan(root: Path) -> dict[str, Any]:
    """Read-only walk collecting every finding. Returns a plain dict of lists/counts."""
    concept_files: list[str] = []          # every .md except .git
    frontmatter_violations: list[str] = []  # should-have-frontmatter files that don't parse
    inbound: dict[str, int] = {}            # rel -> count of body links pointing at it
    dangling: list[dict[str, str]] = []     # {source, target}
    index_repairs: list[str] = []           # concept rels missing from their parent index
    artifact_sources: set[str] = set()      # every path any artifact lists as a source

    all_files = [
        f for f in sorted(root.rglob("*.md")) if ".git" not in f.parts
    ]
    rels = [f.relative_to(root).as_posix() for f in all_files]
    existing = set(rels)

    for f, rel in zip(all_files, rels):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            frontmatter_violations.append(rel)
            continue
        concept_files.append(rel)

        # Frontmatter: index.md and log.md legitimately carry none; everything else must.
        if not _is_index(rel) and not _is_log(rel):
            try:
                doc = split(text)
            except Exception:  # noqa: BLE001 — a bad fence is a violation, not a crash
                doc = None
                frontmatter_violations.append(rel)
            else:
                if doc is None or not str(doc.meta.get("type") or "").strip():
                    frontmatter_violations.append(rel)

        # Outbound body links -> inbound counts + dangling detection. Skip index nav links.
        if not _is_index(rel):
            for m in _REL_MD.finditer(text):
                resolved = _resolve_link(rel, m.group(1))
                if resolved == rel:
                    continue
                inbound[resolved] = inbound.get(resolved, 0) + 1
                if resolved not in existing:
                    dangling.append({"source": rel, "target": resolved})

        # Artifact sources feed dead-knowledge exemption.
        if "/artifacts/" in rel and not _is_index(rel):
            meta = read_meta(f)
            if str(meta.get("type") or "") == "artifact":
                srcs = meta.get("sources")
                if isinstance(srcs, list):
                    artifact_sources.update(str(s) for s in srcs)

    # Index membership: a regular concept must be linked from its parent index.md.
    for rel in concept_files:
        if _is_anchor(rel) or _in_messages(rel):
            continue
        parent = posixpath.dirname(rel)
        index_path = root / parent / "index.md"
        target = posixpath.basename(rel)
        linked = False
        if index_path.is_file():
            try:
                itext = index_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                itext = ""
            linked = re.search(r"\]\((?:[^)\s]*/)?" + re.escape(target) + r"\)", itext) is not None
        if not linked:
            index_repairs.append(rel)

    # Orphans + dead knowledge: concepts nobody's body links to.
    orphans: list[str] = []
    dead: list[str] = []
    for rel in concept_files:
        if _is_anchor(rel) or _in_messages(rel):
            continue
        if inbound.get(rel, 0) > 0:
            continue
        orphans.append(rel)
        if rel not in artifact_sources:
            dead.append(rel)

    return {
        "total_files": len(concept_files),
        "frontmatter_violations": sorted(set(frontmatter_violations)),
        "dangling_links": dangling,
        "index_repairs": sorted(set(index_repairs)),
        "orphans": sorted(orphans),
        "dead": sorted(dead),
    }


def _rel_link(target: str) -> str:
    """Markdown link to ``target`` relative to the report at library/reports/."""
    rel = posixpath.relpath(target, _REPORT_DIR)
    return f"[{target}]({rel})"


def _section(title: str, items: list[str]) -> str:
    if not items:
        return f"## {title} (0)\n\nNone. ✓\n"
    lines = "\n".join(f"* {line}" for line in items)
    return f"## {title} ({len(items)})\n\n{lines}\n"


_PAIR_EXEMPT_TYPES = ("artifact", "report", "inbox", "message", "recipe")


def _similar_pairs(root: Path, idx: Any, threshold: float) -> list[tuple[str, str, float]]:
    """Concept pairs whose title+description embed within ``threshold`` of each other —
    duplicates or contradictions worth a human look. Failure-soft per query."""
    pairs: dict[frozenset[str], float] = {}
    for f in sorted(root.rglob("*.md")):
        rel = f.relative_to(root).as_posix()
        if ".git" in f.parts or _is_index(rel) or _is_log(rel) or _in_messages(rel):
            continue
        meta = read_meta(f)
        if str(meta.get("type") or "") in _PAIR_EXEMPT_TYPES:
            continue
        query = f"{meta.get('title') or ''}. {meta.get('description') or ''}".strip(". ")
        if not query:
            continue
        try:
            hits = idx.search(query, limit=2) or []
        except Exception:  # noqa: BLE001 — advisory sweep, never fail the reconcile
            continue
        for hit in hits:
            other = str(hit.get("path") or "")
            score = float(hit.get("score") or 0.0)
            if other and other != rel and score >= threshold:
                key = frozenset((rel, other))
                pairs[key] = max(pairs.get(key, 0.0), score)
    out = [(sorted(k)[0], sorted(k)[1], v) for k, v in pairs.items()]
    out.sort(key=lambda p: p[2], reverse=True)
    return out


def _render_report(
    scan: dict[str, Any],
    timestamp: str,
    semantic_summary: dict | None,
    similar_pairs: list[tuple[str, str, float]] | None = None,
) -> str:
    similar_pairs = similar_pairs or []
    dangling_lines = [
        f"{_rel_link(d['source'])} -> `{d['target']}` (missing)" for d in scan["dangling_links"]
    ]
    body = "\n".join(
        [
            f"Regenerated {timestamp}. Scanned {scan['total_files']} files.",
            "",
            _section("Frontmatter violations", [_rel_link(p) for p in scan["frontmatter_violations"]]),
            _section("Index repairs applied", [_rel_link(p) for p in scan["index_repairs"]]),
            _section("Dangling links", dangling_lines),
            _section("Orphan concepts (no inbound links)", [_rel_link(p) for p in scan["orphans"]]),
            _section("Dead knowledge (orphan + unsourced by any artifact)", [_rel_link(p) for p in scan["dead"]]),
            _section(
                "Similar pairs (possible duplicates or contradictions — review)",
                [
                    f"{_rel_link(a)} ↔ {_rel_link(b)} (similarity {s:.2f})"
                    for a, b, s in similar_pairs
                ],
            ),
        ]
    )
    if semantic_summary is not None:
        body += f"\n## Semantic reindex\n\n`{semantic_summary}`\n"
    meta = (
        "---\n"
        "type: report\n"
        "title: Brain Health\n"
        "description: Nightly reconcile findings — frontmatter, index, links, orphans, dead knowledge.\n"
        f"timestamp: {timestamp}\n"
        "---\n"
    )
    return f"{meta}\n# Brain Health\n\n{body}\n"


async def run_reconcile(store: Any, semantic_index: Any | None = None) -> dict[str, Any]:
    """Full reconcile pass. Returns a summary dict. Never raises for expected failures."""
    # 1. Pull latest (best-effort — serve/scan local checkout if the remote is down).
    async with store._lock:
        try:
            await to_thread.run_sync(store.repo.pull_rebase)
        except GitError as exc:
            log.warning("reconcile: pull failed, scanning local checkout: %s", exc)

    # 2. Scan (read-only, in a worker thread).
    scan = await to_thread.run_sync(lambda: _scan(store.root))

    # 3. Repair index membership in one commit, if needed.
    repairs = list(scan["index_repairs"])
    if repairs:
        def _repair() -> list[str]:
            changed: list[str] = []
            for rel in repairs:
                meta = read_meta(store.root / rel)
                changed.extend(
                    ensure_indexed(
                        store.root,
                        rel,
                        str(meta.get("title") or ""),
                        str(meta.get("description") or ""),
                    )
                )
            return sorted(set(changed))

        try:
            await store._locked_commit(_repair, "reconcile: repair index membership")
        except GitError as exc:
            log.warning("reconcile: index repair commit failed: %s", exc)

    # 4. Re-embed the whole bundle when semantic is live, then sweep for
    # near-duplicate concept pairs (possible duplicates or contradictions).
    semantic_summary = None
    similar_pairs: list[tuple[str, str, float]] = []
    if semantic_index is not None:
        semantic_summary = await to_thread.run_sync(lambda: semantic_index.full_reindex(store.root))
        threshold = float(getattr(store.settings, "dupe_threshold", 0.80))
        similar_pairs = await to_thread.run_sync(
            lambda: _similar_pairs(store.root, semantic_index, threshold)
        )

    # 5. Write the report back (its own commit).
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = _render_report(scan, timestamp, semantic_summary, similar_pairs)
    try:
        await store.kb_write(_REPORT_PATH, md, "reconcile: brain health report")
    except GitError as exc:
        log.warning("reconcile: report write failed: %s", exc)

    summary = {
        "total_files": scan["total_files"],
        "frontmatter_violations": len(scan["frontmatter_violations"]),
        "index_repairs": len(repairs),
        "dangling_links": len(scan["dangling_links"]),
        "orphans": len(scan["orphans"]),
        "dead": len(scan["dead"]),
        "similar_pairs": len(similar_pairs),
        "report_path": _REPORT_PATH,
    }
    log.info("reconcile: %s", summary)
    return summary
