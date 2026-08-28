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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anyio import to_thread

from .errors import GitError
from .frontmatter import Doc, normalize_meta, read_meta, serialize, split
from .indexer import ensure_indexed
from .kbstore import _parse_ts

log = logging.getLogger("engram.reconcile")

_REL_MD = re.compile(r"\]\((?!https?://|mailto:)([^)\s#]*\.md)(?:#[^)\s]*)?\)")
_REPORT_PATH = "library/reports/brain-health.md"
_REPORT_DIR = "library/reports"
NL = chr(10)


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


def _scan(
    root: Path,
    limits: dict[str, int] | None = None,
    cap: int = 7,
) -> dict[str, Any]:
    """Read-only walk collecting every finding. Returns a plain dict of lists/counts.

    `limits`/`cap` tune the staleness section; both default to the conservative
    built-ins so every existing caller and test keeps its behaviour."""
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

        # Artifact sources feed dead-knowledge exemption — and a manifest that
        # names a missing file is a dangling reference like any broken link.
        if "/artifacts/" in rel and not _is_index(rel):
            meta = read_meta(f)
            if str(meta.get("type") or "") == "artifact":
                srcs = meta.get("sources")
                if isinstance(srcs, list):
                    for s in (str(x) for x in srcs):
                        artifact_sources.add(s)
                        if s not in existing:
                            dangling.append({"source": rel, "target": f"{s} (manifest source)"})

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

    # Orphans + dead knowledge: concepts nobody's body links to. Outputs are
    # exempt — artifacts/reports/recipes carry provenance instead of inbound
    # links, and inbox notes are pre-triage by definition; flagging them is noise.
    _output_types = (
        "artifact", "report", "recipe", "inbox", "imported-conversation",
        "thread", "thread-turn", "presence", "handoff",
    )
    orphans: list[str] = []
    dead: list[str] = []
    for rel in concept_files:
        if _is_anchor(rel) or _in_messages(rel):
            continue
        if inbound.get(rel, 0) > 0:
            continue
        if str(read_meta(root / rel).get("type") or "") in _output_types:
            continue
        orphans.append(rel)
        if rel not in artifact_sources:
            dead.append(rel)

    stale = _stale_candidates(
        root, concept_files, inbound, datetime.now(timezone.utc),
        limits=limits, cap=cap,
    )

    return {
        "total_files": len(concept_files),
        "frontmatter_violations": sorted(set(frontmatter_violations)),
        "dangling_links": dangling,
        "index_repairs": sorted(set(index_repairs)),
        "orphans": sorted(orphans),
        "dead": sorted(dead),
        "stale": stale,
    }


# ============================================================ consolidation
# Inbox captures accumulate and nothing distils them. Other tools auto-summarise;
# we will not, because a server-side LLM would be writing knowledge nobody asked
# for (see the no-server-LLM decision). But CLUSTERING IS MATH: the semantic
# index can group "these six captures are one topic" deterministically, and a
# session can then OFFER the distillation to the human, who decides. Same rule as
# room precipitates: the server points, the human disposes.
#
# SCOPE IS INBOX ONLY, deliberately. Those items are explicitly untriaged - that
# is what an inbox IS. Logs are a historical record and merging them would
# destroy the property that makes them worth keeping; orphan concepts are a
# different problem this scan already reports.
#
# THE RESET: a cluster's signature (its sorted member paths) is remembered
# OUTSIDE the checkout, so a cluster offered once is never offered again.
# Declining must mean declined, or the report shows the same suggestion nightly
# and stops being read - the always-red law again.

_OFFERS_FILE = ".engram-consolidation-offers.json"


def _offers_path(root: Path) -> Path:
    """Beside the checkout, never inside it. An untracked file within the brain
    would trip the clean-checkout guard and block every write - and this is
    server-local operational state, not knowledge. Sitting in the store's parent
    also scopes it per user for free in multiuser mode."""
    return root.parent / _OFFERS_FILE


def _load_offers(root: Path) -> set[str]:
    try:
        import json
        raw = _offers_path(root).read_text(encoding="utf-8")
        return set(json.loads(raw).get("signatures") or [])
    except Exception:  # noqa: BLE001 - a lost memory costs one duplicate offer
        return set()


def _save_offers(root: Path, signatures: set[str]) -> None:
    try:
        import json
        # Bounded: keep the most recent 500 so this cannot grow without limit.
        keep = sorted(signatures)[-500:]
        _offers_path(root).write_text(
            json.dumps({"signatures": keep}), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        log.debug("could not persist consolidation offers", exc_info=True)


def _signature(paths: list[str]) -> str:
    import hashlib
    return hashlib.sha256("|".join(sorted(paths)).encode("utf-8")).hexdigest()[:16]


def _inbox_clusters(
    root: Path,
    idx: Any,
    threshold: float,
    min_size: int = 3,
    cap: int = 3,
) -> list[list[str]]:
    """Groups of inbox captures that look like one topic.

    Connected components over the similarity graph: if A resembles B and B
    resembles C, all three belong together even when A and C never matched
    directly - which is what makes this a cluster rather than a pile of pairs.
    Min size 3 because two similar captures are a coincidence often enough to be
    noise. Failure-soft per query, like every other advisory sweep here."""
    inbox_files = [
        f.relative_to(root).as_posix()
        for f in sorted((root / "inbox").rglob("*.md"))
        if ".git" not in f.parts and not _is_index(f.relative_to(root).as_posix())
        and "/archive/" not in f.relative_to(root).as_posix()
    ]
    if len(inbox_files) < min_size:
        return []

    known = set(inbox_files)
    adjacency: dict[str, set[str]] = {rel: set() for rel in inbox_files}
    for rel in inbox_files:
        meta = read_meta(root / rel)
        query = f"{meta.get('title') or ''}. {meta.get('description') or ''}".strip(". ")
        if not query:
            try:  # a bare capture may carry no description - use its first line
                body = (root / rel).read_text(encoding="utf-8").strip().splitlines()
                query = next((ln for ln in reversed(body) if ln.strip()
                              and not ln.startswith("---")), "")
            except OSError:
                query = ""
        if not query:
            continue
        try:
            hits = idx.search(query, limit=6) or []
        except Exception:  # noqa: BLE001
            continue
        for hit in hits:
            other = str(hit.get("path") or "")
            if other in known and other != rel and float(hit.get("score") or 0.0) >= threshold:
                adjacency[rel].add(other)
                adjacency[other].add(rel)

    seen: set[str] = set()
    clusters: list[list[str]] = []
    for rel in inbox_files:
        if rel in seen:
            continue
        stack, group = [rel], []
        seen.add(rel)
        while stack:
            cur = stack.pop()
            group.append(cur)
            for nxt in adjacency.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if len(group) >= min_size:
            clusters.append(sorted(group))
    # Biggest first: the pile most worth collapsing.
    clusters.sort(key=len, reverse=True)
    return clusters[:cap] if cap > 0 else []


def _fresh_clusters(
    root: Path, idx: Any, threshold: float, min_size: int = 3, cap: int = 3
) -> list[list[str]]:
    """Clusters not already offered, and remembering the ones returned.

    Recording happens HERE rather than on acceptance, because there is no
    acceptance signal to hook: the human may act days later or never. Offering
    once and letting the report go quiet is the honest behaviour - a suggestion
    ignored twice was never going to be taken."""
    found = _inbox_clusters(root, idx, threshold, min_size=min_size, cap=cap)
    if not found:
        return []
    seen = _load_offers(root)
    fresh = [g for g in found if _signature(g) not in seen]
    if fresh:
        _save_offers(root, seen | {_signature(g) for g in fresh})
    return fresh


def _render_clusters(clusters: list[list[str]]) -> str:
    if not clusters:
        return "## Consolidation candidates (0)" + NL + NL + "None. ✓" + NL
    blocks = []
    for group in clusters:
        members = NL.join(f"    * {_rel_link(m)}" for m in group)
        blocks.append(f"* {len(group)} captures that look like one topic:" + NL + members)
    head = f"## Consolidation candidates ({len(clusters)})" + NL + NL
    blurb = (
        "Inbox captures that cluster together. A session can OFFER to distil each "
        "group into one concept - written only on your yes, with the originals "
        "moved to `inbox/archive/`, never deleted. Each group is offered once; "
        "declining is remembered."
    )
    return head + blurb + NL + NL + NL.join(blocks) + NL


# ================================================================ staleness
# THE PROBLEM, in the field's words: high-relevance memories become confidently
# wrong after circumstances change. A June decision saying "blocked on the
# invoice" is retrieved in December with the confidence it had the day it was
# written. Detection here is pure arithmetic — age, type, confidence, and the
# inbound-link count this scan already computes. Judgement stays with the human;
# the server only points.
#
# THE RESET IS THE FEATURE. A list that shows the same seven items every night
# trains the reader to skip it and takes the real signal down with it — the
# always-red law, observed ten times in two days. `reviewed: YYYY-MM-DD` is read
# in preference to `updated`, so confirming a concept makes it quiet without
# editing its body.

_STALE_TYPES = {"decision": 120, "spec": 120, "note": 180, "runbook": 180}
_STALE_EXCLUDE_DIRS = ("inbox/", "threads/", "workspace/", "library/reports/")


def _stale_candidates(
    root: Path,
    concept_files: list[str],
    inbound: dict[str, int],
    now: datetime,
    limits: dict[str, int] | None = None,
    cap: int = 7,
) -> list[dict[str, Any]]:
    """Concepts in ACTIVE USE that nobody has confirmed in a long time.

    Load-bearing is required (inbound >= 1) on purpose: a concept nobody links to
    is an orphan, which this scan reports separately. Staleness is about knowledge
    still being consulted while possibly no longer true — the orphan case is a
    different problem with a different answer.

    Artifacts are excluded because they carry their own computed staleness from
    provenance manifests; logs, indexes, messages and coordination ephemera are
    excluded because age is what they are FOR."""
    limits = limits or _STALE_TYPES
    out: list[dict[str, Any]] = []
    for rel in concept_files:
        if _is_anchor(rel) or _in_messages(rel):
            continue
        if any(rel.startswith(d) for d in _STALE_EXCLUDE_DIRS):
            continue
        links = inbound.get(rel, 0)
        if links < 1:
            continue
        meta = read_meta(root / rel)
        threshold = limits.get(str(meta.get("type") or ""))
        if not threshold:
            continue
        if str(meta.get("confidence") or "settled") != "settled":
            continue  # tentative already reads as provisional; superseded is handled
        stamp = (meta.get("reviewed") or meta.get("updated")
                 or meta.get("timestamp") or meta.get("created"))
        dt = _parse_ts(str(stamp or ""))
        if dt is None:
            continue  # no usable date is not evidence of age
        age = (now - dt).days
        if age < threshold:
            continue
        out.append({"path": rel, "age_days": age, "links": links,
                    "score": age * max(links, 1)})
    # Load-bearing AND old first: a widely-cited stale decision misleads more
    # sessions than a barely-referenced one.
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:cap] if cap > 0 else []


def _render_stale(items: list[dict[str, Any]]) -> str:
    if not items:
        return "## Verify these (0)" + NL + NL + "None. ✓" + NL
    lines = [
        f"* {_rel_link(c['path'])} — {c['age_days']}d since last confirmed, "
        f"{c['links']} inbound link(s)"
        for c in items
    ]
    head = f"## Verify these ({len(items)})" + NL + NL
    blurb = (
        "Still cited, not confirmed in a long time. Check whether each is still "
        "true; if it is, stamp `reviewed: <today>` in its frontmatter and it goes "
        "quiet. If it is not, supersede it."
    )
    return head + blurb + NL + NL + NL.join(lines) + NL


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


# ============================================================ workspace housekeeping
# Coordination ephemera age out: presence heartbeats older than a day are dead
# sessions and get deleted; open handoffs older than a week are stale but kept as
# history. Runs as ONE locked commit inside the nightly reconcile. Failure-soft —
# any single file error is logged and skipped, never aborting the sweep.

_PRESENCE_TTL_HOURS = 24.0    # delete presence records not updated within this window
_HANDOFF_STALE_DAYS = 7.0     # flip still-open handoffs older than this to status: stale
_STATUS_OPEN_RE = re.compile(r"(\*\*Status:\*\*\s*)open", re.IGNORECASE)


def _strip_index_bullet(text: str, basename: str) -> str:
    """Drop any index line whose markdown link targets ``basename`` (a pruned record)."""
    pat = re.compile(r"\]\((?:[^)\s]*/)?" + re.escape(basename) + r"\)")
    kept = [ln for ln in text.splitlines() if not pat.search(ln)]
    return "\n".join(kept).rstrip("\n") + "\n"


def _housekeep(
    root: Path, now: datetime, presence_ttl_hours: float, handoff_stale_days: float
) -> tuple[list[str], dict[str, list[str]]]:
    """Mutating lifecycle sweep (runs inside a locked commit): delete stale presence
    records + their parent-index bullet, and mark old open handoffs stale (never delete).
    Never touches fresh presence, already-stale/undated handoffs, or any thread. Returns
    (changed_rel_paths, {"pruned_presence": [...], "staled_handoffs": [...]})."""
    changed: list[str] = []
    pruned_presence: list[str] = []
    staled_handoffs: list[str] = []

    # --- presence prune: a heartbeat we can't prove is fresh is a dead session ---
    pdir = root / "workspace" / "presence"
    if pdir.is_dir():
        index_path = pdir / "index.md"
        index_text: str | None = None
        if index_path.is_file():
            try:
                index_text = index_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                index_text = None
        index_dirty = False
        for f in sorted(pdir.glob("*.md")):
            if f.name == "index.md":
                continue
            meta = read_meta(f)
            if str(meta.get("type") or "") != "presence":
                continue
            dt = _parse_ts(str(meta.get("updated") or ""))
            # Keep only records provably within the TTL; undated/unparseable = stale.
            if dt is not None and (now - dt).total_seconds() <= presence_ttl_hours * 3600:
                continue
            rel = f.relative_to(root).as_posix()
            try:
                f.unlink()
            except OSError as exc:
                log.warning("reconcile: could not prune presence %s: %s", rel, exc)
                continue
            changed.append(rel)
            pruned_presence.append(rel)
            if index_text is not None:
                new_text = _strip_index_bullet(index_text, f.name)
                if new_text != index_text:
                    index_text = new_text
                    index_dirty = True
        if index_dirty and index_text is not None:
            try:
                with open(index_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(index_text)
                changed.append(index_path.relative_to(root).as_posix())
            except OSError as exc:
                log.warning("reconcile: could not rewrite presence index: %s", exc)

    # --- handoff stale-marking: keep the record, flip its status (history, not trash) ---
    hdir = root / "workspace" / "handoffs"
    if hdir.is_dir():
        for f in sorted(hdir.glob("*.md")):
            if f.name == "index.md":
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                doc = split(text)
            except Exception:  # noqa: BLE001 — a corrupt handoff is left untouched
                doc = None
            if doc is None:
                continue
            meta = normalize_meta(doc.meta)
            if str(meta.get("type") or "") != "handoff" or str(meta.get("status") or "") != "open":
                continue
            dt = _parse_ts(str(meta.get("created") or ""))
            if dt is None or (now - dt).total_seconds() <= handoff_stale_days * 86400:
                continue  # can't age it, or still recent -> leave open
            meta["status"] = "stale"
            body = _STATUS_OPEN_RE.sub(r"\g<1>stale", doc.body, count=1)
            try:
                with open(f, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(serialize(Doc(meta=meta, body=body)))
            except OSError as exc:
                log.warning("reconcile: could not stale handoff %s: %s", f.name, exc)
                continue
            rel = f.relative_to(root).as_posix()
            changed.append(rel)
            staled_handoffs.append(rel)

    return changed, {"pruned_presence": pruned_presence, "staled_handoffs": staled_handoffs}


def _render_housekeeping(hk: dict[str, list[str]]) -> str:
    """A 'Workspace housekeeping' report section. Pruned/staled paths are rendered as
    code spans (not links) so a deleted presence file never becomes a dangling link."""
    pruned = hk.get("pruned_presence", [])
    staled = hk.get("staled_handoffs", [])
    lines = [
        f"## Workspace housekeeping",
        "",
        f"* Pruned {len(pruned)} stale presence record(s).",
    ]
    lines += [f"    * `{p}`" for p in pruned]
    lines.append(f"* Marked {len(staled)} open handoff(s) stale.")
    lines += [f"    * `{p}`" for p in staled]
    return "\n".join(lines) + "\n"


def _render_report(
    scan: dict[str, Any],
    timestamp: str,
    semantic_summary: dict | None,
    similar_pairs: list[tuple[str, str, float]] | None = None,
    housekeeping: dict[str, list[str]] | None = None,
    clusters: list[list[str]] | None = None,
) -> str:
    similar_pairs = similar_pairs or []
    clusters = clusters or []
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
            _render_stale(scan.get("stale") or []),
            _render_clusters(clusters),
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
    if housekeeping is not None:
        body += "\n" + _render_housekeeping(housekeeping)
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
    cfg = getattr(store, "settings", None)
    stale_limits = {
        "decision": getattr(cfg, "stale_after_days_decision", 120),
        "spec": getattr(cfg, "stale_after_days_decision", 120),
        "note": getattr(cfg, "stale_after_days_note", 180),
        "runbook": getattr(cfg, "stale_after_days_note", 180),
    }
    stale_cap = getattr(cfg, "stale_report_limit", 7)
    scan = await to_thread.run_sync(
        lambda: _scan(store.root, limits=stale_limits, cap=stale_cap)
    )

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

    # 3.5. Workspace housekeeping — prune dead presence, mark old handoffs stale
    # (one locked commit; before reindex so pruned files never re-embed).
    presence_ttl_hours = float(getattr(store.settings, "presence_ttl_hours", _PRESENCE_TTL_HOURS))
    handoff_stale_days = float(getattr(store.settings, "handoff_stale_days", _HANDOFF_STALE_DAYS))
    housekeeping: dict[str, list[str]] = {"pruned_presence": [], "staled_handoffs": []}
    now = datetime.now(timezone.utc)
    hk_state: dict[str, dict[str, list[str]]] = {}

    def _do_housekeep() -> list[str]:
        changed, hk_summary = _housekeep(store.root, now, presence_ttl_hours, handoff_stale_days)
        hk_state["summary"] = hk_summary
        return changed

    try:
        await store._locked_commit(_do_housekeep, "reconcile: workspace housekeeping")
        housekeeping = hk_state.get("summary", housekeeping)
    except GitError as exc:
        log.warning("reconcile: housekeeping commit failed: %s", exc)

    # 4. Re-embed the whole bundle when semantic is live, then sweep for
    # near-duplicate concept pairs (possible duplicates or contradictions).
    semantic_summary = None
    similar_pairs: list[tuple[str, str, float]] = []
    clusters: list[list[str]] = []
    if semantic_index is not None:
        semantic_summary = await to_thread.run_sync(lambda: semantic_index.full_reindex(store.root))
        threshold = float(getattr(store.settings, "dupe_threshold", 0.80))
        clusters = await to_thread.run_sync(
            lambda: _fresh_clusters(
                store.root, semantic_index, threshold,
                min_size=getattr(cfg, "consolidate_min_cluster", 3),
                cap=getattr(cfg, "consolidate_max_clusters", 3),
            )
        )
        similar_pairs = await to_thread.run_sync(
            lambda: _similar_pairs(store.root, semantic_index, threshold)
        )

    # 5. Write the report back (its own commit).
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = _render_report(scan, timestamp, semantic_summary, similar_pairs, housekeeping,
                        clusters)
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
        "stale": len(scan.get("stale") or []),
        "clusters": len(clusters),
        "pruned_presence": len(housekeeping["pruned_presence"]),
        "staled_handoffs": len(housekeeping["staled_handoffs"]),
        "report_path": _REPORT_PATH,
    }
    log.info("reconcile: %s", summary)
    return summary
