"""bench_embeddings.py — head-to-head of embedding models on the real brain.

Indexes a SNAPSHOT of the brain (a local git clone, so the live checkout is never
touched) into one fresh Qdrant collection per model, then runs a fixed probe set
through two paths:
  - semantic only  (SemanticIndex.search) — the model's own retrieval quality
  - full kb_search (hybrid text+vector fusion, expansion ON) — what users actually get
and reports recall@1, recall@3, MRR, full-index time and query latency.

Probes are FROZEN before any model is run: the original 16 from bench_search.py plus
34 written from a random sample of concept titles across the whole brain, phrased the
way a person would ask rather than copying the concept's own wording.

Usage (from server/):
    uv run --no-sync python scripts/bench_embeddings.py --snapshot <dir>
    uv run --no-sync python scripts/bench_embeddings.py --snapshot <dir> --keep   # keep collections
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_search import PROBES as ORIGINAL_PROBES  # noqa: E402

from engram_server.config import get_settings  # noqa: E402
from engram_server.kbstore import KBStore  # noqa: E402
from engram_server.semantic import EMBEDDINGGEMMA  # noqa: E402

NEW_PROBES: list[tuple[str, str]] = [
    ("how should I lay out a company with many workstreams in the brain", "organizing-projects"),
    ("which design plugin detects ai slop in a ui", "impeccable"),
    ("mapping dom elements back to source code for a live edit mode", "building-live-mode"),
    ("which statistics in the design tooling research turned out to be made up", "verification-and-fabrications"),
    ("arduino light that shows when claude is waiting for me", "session-monitor-system"),
    ("first call about the dream video project with shreyas", "2026-09-14-kickoff-call"),
    ("spending limit for running the dream harness overnight", "overnight-budget"),
    ("what subjects should we post to get real views", "what-to-make-bets"),
    ("360 degree camera lora results", "camera-lora-bench"),
    ("how do i drive the dream chat from the terminal", "operating-the-dream-chat"),
    ("using the person's own face and voice in their dream", "dreamer-photo-voice-experiment"),
    ("rethinking the storyboard harness design from first principles", "the-harness-rethought"),
    ("should the interview tell candidates they are talking to an ai", "remove-candidate-ai-disclosure"),
    ("capturing how the founder writes", "voice-capture-reveal-at-end"),
    ("hosting multi-user engram on my own computer instead of a cloud vm", "v2-go-pc-hosted"),
    ("splitting income into virtual envelopes across two bank accounts", "money-buckets"),
    ("removing fake white fingernails from generated hand animation", "white-nail-cleanup"),
    ("turning a photo into an animated sprite sheet with gemini", "gemini-pipeline"),
    ("making a short multi-shot story video locally on the gpu", "h3-local-story-video-recipe"),
    ("why we stopped using self-hosted qwen to judge images", "drop-open-image-judges"),
    ("admin database tab says jwk not found", "neon-data-api-binding"),
    ("deploying a frontend preview to cloudflare pages", "pages-preview-deploy"),
    ("what we need the backend engineers to build for the admin console", "backend-requests-next-version"),
    ("who builds which parts of the form ui", "tanvi-form-division"),
    ("does keeping reasoning between turns improve quality", "2026-09-24-team-call"),
    ("competitor profile pages like linktree and read.cv", "research/profile-pages"),
    ("how do studios actually build a brand identity", "brand-process"),
    ("blockers when standing up a brand new dev environment", "standing-up-a-fresh-dev-env"),
    ("comparing minimax h3 variants for the sprite migration", "h3-variant-bench-for-wan22"),
    ("why i feel unproductive despite working a lot", "output-vs-conversion"),
    ("art installation where sketches became album covers", "sound-canvas"),
    ("the respondent screen remounting on every state change", "form-stage-architecture"),
    ("dark lamplit visual style for stellar", "night-desk-design-language"),
    ("rebuild the opinion 2x2 as a drag-a-puck board", "spatial-input-redesign"),
]
PROBES = ORIGINAL_PROBES + NEW_PROBES

MODELS = {
    "bge": "BAAI/bge-small-en-v1.5",
    "gemma": EMBEDDINGGEMMA,
}


def _rank(paths: list[str], needle: str) -> int | None:
    for i, p in enumerate(paths):
        if needle in p:
            return i + 1
    return None


def _metrics(ranks: list[int | None]) -> tuple[float, float, float]:
    n = len(ranks)
    r1 = sum(1 for r in ranks if r == 1) / n
    r3 = sum(1 for r in ranks if r is not None and r <= 3) / n
    mrr = sum(1 / r for r in ranks if r is not None) / n
    return r1, r3, mrr


async def run_model(tag: str, model: str, snapshot: Path, keep: bool) -> dict:
    settings = get_settings().model_copy(update={
        "brain_path": snapshot,
        "qdrant_collection": f"engram-bench-{tag}",
        "embed_model": model,
        "rerank_enabled": False,
    })
    store = KBStore(settings)
    await store.start()
    sem = store.semantic
    client = sem._client()
    if client.collection_exists(settings.qdrant_collection):
        client.delete_collection(settings.qdrant_collection)

    t = time.perf_counter()
    stats = sem.full_reindex(snapshot)
    index_s = time.perf_counter() - t

    sem_ranks, hyb_ranks, q_ms = [], [], []
    for query, needle in PROBES:
        t = time.perf_counter()
        hits = sem.search(query, limit=8) or []
        q_ms.append((time.perf_counter() - t) * 1000)
        sem_ranks.append(_rank([h["path"] for h in hits], needle))
        results = await store.kb_search(query, limit=8, expand=True)
        hyb_ranks.append(_rank([r["path"] for r in results], needle))

    if not keep:
        client.delete_collection(settings.qdrant_collection)
    return {"tag": tag, "model": model, "index_s": index_s, "stats": stats,
            "sem": sem_ranks, "hyb": hyb_ranks, "q_ms": q_ms}


def _present(snapshot: Path) -> list[bool]:
    paths = [p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*.md")]
    return [any(n in p for p in paths) for _q, n in PROBES]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", choices=sorted(MODELS), action="append")
    args = ap.parse_args()

    present = _present(args.snapshot)
    missing = [n for (_q, n), ok in zip(PROBES, present) if not ok]
    if missing:
        print("probe targets missing from snapshot:", missing)
        return 2

    runs = [await run_model(tag, MODELS[tag], args.snapshot, args.keep)
            for tag in (args.only or MODELS)]

    print(f"\nprobes={len(PROBES)}  (original={len(ORIGINAL_PROBES)}, new={len(NEW_PROBES)})\n")
    print(f"{'model':<7} {'path':<9} {'R@1':>6} {'R@3':>6} {'MRR':>6}   index      query p50/p95")
    for r in runs:
        p50 = statistics.median(r["q_ms"])
        p95 = sorted(r["q_ms"])[int(0.95 * (len(r["q_ms"]) - 1))]
        for path in ("sem", "hyb"):
            r1, r3, mrr = _metrics(r[path])
            extra = f"   {r['index_s']:6.0f}s   {p50:5.0f}/{p95:4.0f} ms" if path == "sem" else ""
            print(f"{r['tag']:<7} {path:<9} {r1:6.3f} {r3:6.3f} {mrr:6.3f}{extra}")
    print("\nsubsets (hybrid R@1): original | new")
    no = len(ORIGINAL_PROBES)
    for r in runs:
        o = _metrics(r["hyb"][:no])[0]
        n = _metrics(r["hyb"][no:])[0]
        print(f"  {r['tag']:<7} {o:.3f} | {n:.3f}")

    print("\nper-probe ranks (semantic / hybrid):")
    print(f"  {'probe':<52} " + "  ".join(f"{r['tag']:>9}" for r in runs))
    for i, (q, _n) in enumerate(PROBES):
        cells = "  ".join(f"{str(r['sem'][i] or '-'):>4}/{str(r['hyb'][i] or '-'):<4}" for r in runs)
        print(f"  {q[:52]:<52} {cells}")
    for r in runs:
        print(f"\n{r['tag']} index stats: {r['stats']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
