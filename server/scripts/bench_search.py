"""bench_search.py — a measuring stick for kb_search, not a test.

Runs a fixed set of (query, expected-path-substring) probes over the REAL brain
checkout (``ENGRAM_BRAIN_PATH`` / settings.brain_path) and reports recall@k + MRR
with multi-query expansion ON vs OFF, so we can SEE whether the wave-2 levers
(hybrid fusion, query expansion, time-window) actually move retrieval. It also
prints which engine served each probe (hybrid | semantic | text) — the fastest way
to tell at a glance whether Qdrant is live for this run or we are text-only.

Usage (from server/):
    uv run --no-sync python scripts/bench_search.py
    uv run --no-sync python scripts/bench_search.py --k 5 --limit 10
    uv run --no-sync python scripts/bench_search.py --resume     # reuse cached probe runs

A probe counts as a hit at rank r if the r-th result's path CONTAINS the expected
substring (robust to exact filenames/dirs moving). recall@k = hits within top-k /
scored probes; MRR = mean(1/rank). Probes whose expected file is absent from this
brain are reported as SKIPPED and left out of the denominator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Allow `python scripts/bench_search.py` without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram_server.config import get_settings  # noqa: E402
from engram_server.kbstore import KBStore  # noqa: E402

# (query, expected path substring) — derived from real projects/engram concepts.
PROBES: list[tuple[str, str]] = [
    ("why did we choose oauth instead of a bearer token", "oauth-allowlist"),
    ("what embedding model does the semantic search use", "local-embeddings"),
    ("why invent our own okf knowledge format", "okf-format"),
    ("cloudflare access explorer authentication", "cf-access-explorer"),
    ("mcp streamable http transport choice", "mcp-transport"),
    ("nightly reconcile in-process scheduler", "inprocess-scheduler"),
    ("git push failed while offline soft failure", "soft-push-failure"),
    ("shareable artifact system design", "artifact-system"),
    ("v1 architecture spec", "v1-architecture"),
    ("brain navigator widget suite", "brain-navigator"),
    ("basic memory comparison notes", "basic-memory-comparison"),
    ("agentmemory research recommendations", "agentmemory"),
    ("why a standalone service instead of folding into helix", "standalone-service"),
    ("should we push the engram code repo to github", "github-remote"),
    ("text search v1 scorer weighted fields", "search-text-v1"),
    ("hiren's stack and tools", "self/stack"),
]


def _rank_of(results: list[dict], needle: str) -> int | None:
    for i, r in enumerate(results):
        if needle in r.get("path", ""):
            return i + 1  # 1-based
    return None


async def _run_config(store: KBStore, expand: bool, k: int, limit: int) -> dict:
    rows = []
    for query, needle in PROBES:
        results = await store.kb_search(query, limit=limit, expand=expand)
        rank = _rank_of(results, needle)
        engine = results[0]["engine"] if results else "-"
        rows.append({"query": query, "needle": needle, "rank": rank, "engine": engine, "k": k})
    return {"expand": expand, "rows": rows}


def _score(rows: list[dict], scored_needles: set[str], k: int) -> tuple[float, float, int]:
    scored = [r for r in rows if r["needle"] in scored_needles]
    if not scored:
        return 0.0, 0.0, 0
    hits_at_k = sum(1 for r in scored if r["rank"] is not None and r["rank"] <= k)
    mrr = sum((1.0 / r["rank"]) for r in scored if r["rank"] is not None) / len(scored)
    return hits_at_k / len(scored), mrr, len(scored)


def _present_needles(root: Path) -> set[str]:
    """Which probe targets actually exist in this brain (so absent ones don't count as misses)."""
    all_paths = [p.relative_to(root).as_posix() for p in root.rglob("*.md")]
    present = set()
    for _q, needle in PROBES:
        if any(needle in path for path in all_paths):
            present.add(needle)
    return present


async def main() -> int:
    ap = argparse.ArgumentParser(description="Measure kb_search recall@k / MRR, expansion ON vs OFF.")
    ap.add_argument("--k", type=int, default=3, help="recall@k cutoff (default 3)")
    ap.add_argument("--limit", type=int, default=8, help="kb_search result limit (default 8)")
    ap.add_argument("--resume", action="store_true", help="reuse cached probe runs if present")
    args = ap.parse_args()

    settings = get_settings()
    root = settings.brain_path
    if not root.exists():
        print(f"brain checkout not found at {root} — set ENGRAM_BRAIN_PATH or clone first.")
        return 2

    store = KBStore(settings)
    await store.start()

    present = _present_needles(root)
    skipped = [n for _q, n in PROBES if n not in present]

    # Cache lives in the system temp dir, never the repo, so re-runs leave no trace.
    cache_path = Path(tempfile.gettempdir()) / "engram_bench_search_cache.json"
    cache = {}
    if args.resume and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}

    configs = {}
    for expand in (True, False):
        key = f"expand={expand}"
        if args.resume and key in cache:
            configs[expand] = cache[key]
        else:
            configs[expand] = await _run_config(store, expand, args.k, args.limit)
    cache = {f"expand={e}": c for e, c in configs.items()}
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    engines = {r["engine"] for c in configs.values() for r in c["rows"]}
    mode = "hybrid/semantic live" if (engines & {"hybrid", "semantic"}) else "TEXT-ONLY (no Qdrant)"

    print(f"\nbench_search - brain={root}  probes={len(PROBES)}  scored={len(present)}  skipped={len(skipped)}")
    print(f"engine mode this run: {mode}\n")

    # Per-probe detail (expansion ON).
    print(f"{'rank':>4}  {'engine':<9}  {'target':<26}  query")
    print("-" * 88)
    for r in configs[True]["rows"]:
        if r["needle"] in skipped:
            mark = "SKIP"
        else:
            mark = str(r["rank"]) if r["rank"] is not None else "miss"
        print(f"{mark:>4}  {r['engine']:<9}  {r['needle']:<26}  {r['query'][:34]}")

    # Aggregate table: expansion ON vs OFF.
    print(f"\n{'config':<14}  {'recall@1':>9}  {'recall@'+str(args.k):>9}  {'MRR':>6}  {'n':>3}")
    print("-" * 52)
    for expand in (True, False):
        rows = configs[expand]["rows"]
        r1, _m1, _n = _score(rows, present, 1)
        rk, mrr, n = _score(rows, present, args.k)
        label = "expand ON" if expand else "expand OFF"
        print(f"{label:<14}  {r1:>9.3f}  {rk:>9.3f}  {mrr:>6.3f}  {n:>3}")

    if skipped:
        print(f"\nskipped (target absent in this brain): {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
