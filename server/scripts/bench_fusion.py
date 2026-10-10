"""bench_fusion.py — tune kb_search's text+vector fusion, per embedding model.

Phase 1 (slow, once): index the brain snapshot with each model (collections kept),
then capture, for every probe and every query variant, the raw ranked lists from the
vector engine and the text engine. Saved to --cache.

Phase 2 (instant): replay kb_search's fusion offline over a grid of settings —
semantic weight, RRF k, and whether the vector side also runs the expansion variants.
The replica is only trusted if the CURRENT settings reproduce the live kb_search
numbers from bench_embeddings.py exactly.

Probes are split into a tune half and a holdout half (fixed seed, stratified across
the original 16 and the new 34). The best setting is chosen on tune only; holdout is
what gets reported, so the result isn't just fit to these 50 questions.

Usage (from server/):
    uv run --no-sync python scripts/bench_fusion.py --snapshot <dir> --cache <file> --capture
    uv run --no-sync python scripts/bench_fusion.py --cache <file>
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_embeddings import MODELS, NEW_PROBES, ORIGINAL_PROBES, PROBES  # noqa: E402

from engram_server.errors import KBError  # noqa: E402
from engram_server.search import query_variants, search as run_text, union_max  # noqa: E402

CAND = 24  # kb_search(limit=8) -> cand = min(25, max(8*3, 10))
LIMIT = 8


async def capture(snapshot: Path, cache: Path) -> None:
    from engram_server.config import get_settings
    from engram_server.kbstore import KBStore

    out: dict = {"text": [], "sem": {}}
    for q, _n in PROBES:
        lists = []
        for i, v in enumerate(query_variants(q)):
            try:
                lists.append([(r["path"], r["score"]) for r in run_text(snapshot, v, limit=CAND)])
            except KBError:
                if i == 0:
                    raise
                lists.append(None)
        out["text"].append(lists)

    for tag, model in MODELS.items():
        settings = get_settings().model_copy(update={
            "brain_path": snapshot, "qdrant_collection": f"engram-bench-{tag}",
            "embed_model": model, "rerank_enabled": False,
        })
        store = KBStore(settings)
        await store.start()
        sem = store.semantic
        client = sem._client()
        if client.collection_exists(settings.qdrant_collection):
            client.delete_collection(settings.qdrant_collection)
        print(f"indexing {tag}...", flush=True)
        sem.full_reindex(snapshot)
        rows = []
        for q, _n in PROBES:
            per_variant = []
            for v in query_variants(q):
                hits = sem.search(v, limit=CAND)
                per_variant.append(None if hits is None else [(h["path"], h["score"]) for h in hits])
            rows.append(per_variant)
        out["sem"][tag] = rows
        print(f"captured {tag}", flush=True)
    cache.write_text(json.dumps(out), encoding="utf-8")


def _as_rows(lst):
    return [{"path": p, "score": s} for p, s in lst]


def fuse(text_lists, sem_lists, w: float, k: int, sem_expand: bool) -> list[str]:
    text = union_max([_as_rows(lst) for lst in text_lists if lst is not None])
    chosen = sem_lists if sem_expand else sem_lists[:1]
    ok = [lst for lst in chosen if lst is not None]
    sem = union_max([_as_rows(lst) for lst in ok]) if ok else None
    if not sem:
        return [r["path"] for r in text][:LIMIT]
    if not text:
        return [r["path"] for r in sem][:LIMIT]
    scores: dict[str, float] = {}
    for weight, ranked in ((w, sem), (1.0, text)):
        for rank, r in enumerate(ranked):
            scores[r["path"]] = scores.get(r["path"], 0.0) + weight / (k + rank + 1)
    return sorted(scores, key=lambda p: scores[p], reverse=True)[:LIMIT]


def metrics(ranks):
    n = len(ranks)
    return (sum(r == 1 for r in ranks) / n,
            sum(r is not None and r <= 3 for r in ranks) / n,
            sum(1 / r for r in ranks if r is not None) / n)


def rank_of(paths, needle):
    return next((i + 1 for i, p in enumerate(paths) if needle in p), None)


def evaluate(data, tag, idx, w, k, sem_expand):
    ranks = []
    for i in idx:
        paths = fuse(data["text"][i], data["sem"][tag][i], w, k, sem_expand)
        ranks.append(rank_of(paths, PROBES[i][1]))
    return metrics(ranks)


def split() -> tuple[list[int], list[int]]:
    rng = random.Random(20261010)
    tune, hold = [], []
    for block in (range(len(ORIGINAL_PROBES)), range(len(ORIGINAL_PROBES), len(PROBES))):
        ids = list(block)
        rng.shuffle(ids)
        half = len(ids) // 2
        tune += ids[:half]
        hold += ids[half:]
    return sorted(tune), sorted(hold)


def report(cache: Path) -> None:
    data = json.loads(cache.read_text(encoding="utf-8"))
    every = list(range(len(PROBES)))
    tune, hold = split()
    grid = list(itertools.product([0.5, 1, 1.5, 2, 3, 5, 10], [5, 10, 20, 30, 60], [True, False]))

    fmt = lambda m: f"{m[0]:.3f} {m[1]:.3f} {m[2]:.3f}"  # noqa: E731
    print(f"probes={len(PROBES)}  tune={len(tune)}  holdout={len(hold)}  grid={len(grid)}")
    print("columns: R@1 R@3 MRR\n")
    for tag in data["sem"]:
        current = evaluate(data, tag, every, 1, 60, True)
        print(f"[{tag}] REPLICA CHECK, current settings (w=1, k=60, expand=all), all 50: {fmt(current)}")
        sem_only = metrics([rank_of(
            [r["path"] for r in union_max([_as_rows(lst) for lst in data["sem"][tag][i][:1] if lst])][:LIMIT],
            PROBES[i][1]) for i in every])
        print(f"[{tag}] vector only (raw query), all 50: {fmt(sem_only)}")
        scored = sorted(
            ((evaluate(data, tag, tune, w, k, e), (w, k, e)) for w, k, e in grid),
            key=lambda x: (x[0][2], x[0][0]), reverse=True,
        )
        best_m, (w, k, e) = scored[0]
        print(f"[{tag}] best on TUNE: w={w} k={k} sem_expand={e}  tune={fmt(best_m)}")
        print(f"[{tag}]   holdout: current={fmt(evaluate(data, tag, hold, 1, 60, True))}"
              f"  best={fmt(evaluate(data, tag, hold, w, k, e))}")
        print(f"[{tag}]   all 50 : current={fmt(current)}  best={fmt(evaluate(data, tag, every, w, k, e))}")
        print(f"[{tag}]   top 5 on tune:", [f"w={c[1][0]} k={c[1][1]} e={c[1][2]} mrr={c[0][2]:.3f}" for c in scored[:5]])
        print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--capture", action="store_true")
    args = ap.parse_args()
    if args.capture:
        asyncio.run(capture(args.snapshot, args.cache))
    report(args.cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
