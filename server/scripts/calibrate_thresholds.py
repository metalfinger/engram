"""calibrate_thresholds.py — map Engram's cosine cutoffs from one embedding model to another.

Ranking survives a model swap (it only compares scores to each other), but three
features compare a score to a FIXED number tuned on bge-small:
  - dupe_threshold (0.80)           kb_write near-duplicate warning + nightly similar-pairs
  - common_ground threshold (0.55)  People-pane / kb_common_ground work overlap
  - artifact_drift_threshold (0.5)  artifact rebuild guard
Each is recomputed here exactly the way the feature computes its score, under both
models, and the new model's cutoff is chosen to fire on the SAME SHARE of cases
(percentile matching). Agreement between the two models' flagged sets is reported,
so a matched rate that flags different things doesn't pass for calibration.

Needs the collections bench_fusion.py --capture leaves behind (engram-bench-<tag>).

Usage (from server/):
    uv run --no-sync python scripts/calibrate_thresholds.py --snapshot <dir>
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_embeddings import MODELS  # noqa: E402

from engram_server.config import get_settings  # noqa: E402
from engram_server.frontmatter import normalize_meta, split  # noqa: E402
from engram_server.semantic import SemanticIndex  # noqa: E402

BGE_CUTOFFS = {"dupe": 0.80, "common_ground": 0.55, "drift": 0.5}
_DUPE_EXEMPT = {"artifact", "report", "inbox", "message"}
_SKIP_PARTS = {".git", "messages", "threads", "workspace", "inbox"}


def _meta(path: Path) -> dict:
    doc = split(path.read_text(encoding="utf-8", errors="ignore"))
    return normalize_meta(doc.meta) if doc is not None else {}


def _concepts(snapshot: Path) -> list[tuple[str, dict]]:
    out = []
    for f in sorted(snapshot.rglob("*.md")):
        rel = f.relative_to(snapshot).as_posix()
        if f.name in ("index.md", "log.md") or _SKIP_PARTS & set(f.relative_to(snapshot).parts):
            continue
        out.append((rel, _meta(f)))
    return out


def _index(tag: str, snapshot: Path) -> SemanticIndex:
    s = get_settings().model_copy(update={
        "brain_path": snapshot, "qdrant_collection": f"engram-bench-{tag}", "embed_model": MODELS[tag],
    })
    return SemanticIndex(s)


def _points(idx: SemanticIndex) -> list[tuple[str, str, np.ndarray]]:
    """(path, project, unit vector) for every chunk, in the store's scroll order."""
    out, offset = [], None
    while True:
        recs, offset = idx._client().scroll(
            collection_name=idx.collection, with_vectors=True, with_payload=True,
            limit=1000, offset=offset,
        )
        for r in recs:
            v = np.asarray(r.vector, dtype=np.float32)
            out.append((r.payload["path"], r.payload.get("project") or "", v / np.linalg.norm(v)))
        if offset is None:
            return out


def dupe_scores(idx: SemanticIndex, concepts) -> dict[str, tuple[float, str]]:
    """Exactly kb_write's _near_duplicate: 'title. description' as a QUERY, best other hit."""
    out = {}
    for rel, meta in concepts:
        if str(meta.get("type") or "") in _DUPE_EXEMPT:
            continue
        q = f"{meta.get('title') or ''}. {meta.get('description') or ''}".strip(". ")
        if not q:
            continue
        hits = idx.search(q, limit=3) or []
        best = next(((float(h["score"]), h["path"]) for h in hits if h["path"] != rel), (0.0, ""))
        out[rel] = best
    return out


def common_ground_scores(points) -> dict[str, float]:
    """common_ground: a concept's first-seen chunk vector vs the best chunk elsewhere.
    'Elsewhere' = a different project — the in-brain stand-in for another person's work."""
    first: dict[str, tuple[str, np.ndarray]] = {}
    for path, proj, v in points:
        first.setdefault(path, (proj, v))
    mat = np.stack([v for _p, _pr, v in points])
    projs = np.array([pr for _p, pr, _v in points])
    out = {}
    for path, (proj, v) in first.items():
        sims = mat @ v
        sims[projs == proj] = -1.0
        out[path] = float(sims.max())
    return out


def centroids(points) -> dict[str, np.ndarray]:
    groups: dict[str, list[np.ndarray]] = {}
    for path, _proj, v in points:
        groups.setdefault(path, []).append(v)
    return {p: np.mean(vs, axis=0) for p, vs in groups.items()}


def _cos(a, b) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def drift_scores(cents, concepts) -> tuple[list[float], list[tuple[str, float]]]:
    """Background: centroid similarity of random UNRELATED concept pairs (what a drifted
    artifact looks like). Grounded: each real artifact vs the centroid of its sources."""
    rng = random.Random(7)
    paths = sorted(cents)
    background = []
    for _ in range(20000):
        a, b = rng.sample(paths, 2)
        background.append(_cos(cents[a], cents[b]))
    grounded = []
    for rel, meta in concepts:
        if str(meta.get("type") or "") != "artifact" or rel not in cents:
            continue
        srcs = [s for s in (meta.get("sources") or []) if isinstance(s, str) and s in cents]
        if srcs:
            grounded.append((rel, _cos(cents[rel], np.mean([cents[s] for s in srcs], axis=0))))
    return background, grounded


def matched_cutoff(old_scores, old_cut, new_scores, above: bool = True) -> tuple[float, float]:
    """New cutoff that keeps the same fraction of cases on the same side of the line."""
    old = np.asarray(old_scores)
    rate = float(np.mean(old >= old_cut))
    return float(np.quantile(np.asarray(new_scores), 1.0 - rate)), rate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    args = ap.parse_args()
    concepts = _concepts(args.snapshot)
    res = {}
    for tag in MODELS:
        idx = _index(tag, args.snapshot)
        pts = _points(idx)
        print(f"{tag}: {len(pts)} chunks", flush=True)
        res[tag] = {
            "dupe": dupe_scores(idx, concepts),
            "cg": common_ground_scores(pts),
            "drift": drift_scores(centroids(pts), concepts),
        }
    b, g = res["bge"], res["gemma"]

    # 1. near-duplicate
    keys = sorted(set(b["dupe"]) & set(g["dupe"]))
    bs = [b["dupe"][k][0] for k in keys]
    gs = [g["dupe"][k][0] for k in keys]
    cut, rate = matched_cutoff(bs, BGE_CUTOFFS["dupe"], gs)
    fb = {k for k in keys if b["dupe"][k][0] >= BGE_CUTOFFS["dupe"]}
    fg = {k for k in keys if g["dupe"][k][0] >= cut}
    print(f"\nDUPE (n={len(keys)}): bge flags {len(fb)} ({rate:.1%}) at 0.80 -> gemma cutoff {cut:.3f}")
    print(f"  bge score quartiles {np.round(np.quantile(bs, [.25, .5, .75, .95]), 3)}")
    print(f"  gemma score quartiles {np.round(np.quantile(gs, [.25, .5, .75, .95]), 3)}")
    print(f"  agreement: both {len(fb & fg)}, bge-only {len(fb - fg)}, gemma-only {len(fg - fb)}")
    for k in sorted(fb ^ fg)[:12]:
        side = "bge-only " if k in fb else "gemma-only"
        print(f"    {side} {k}  ~ {(b if k in fb else g)['dupe'][k][1]}"
              f"  (bge {b['dupe'][k][0]:.3f}, gemma {g['dupe'][k][0]:.3f})")

    # 2. common ground
    keys = sorted(set(b["cg"]) & set(g["cg"]))
    bs = [b["cg"][k] for k in keys]
    gs = [g["cg"][k] for k in keys]
    cut, rate = matched_cutoff(bs, BGE_CUTOFFS["common_ground"], gs)
    fb = {k for k in keys if b["cg"][k] >= 0.55}
    fg = {k for k in keys if g["cg"][k] >= cut}
    print(f"\nCOMMON GROUND (n={len(keys)}): bge matches {rate:.1%} at 0.55 -> gemma cutoff {cut:.3f}")
    print(f"  agreement: both {len(fb & fg)}, bge-only {len(fb - fg)}, gemma-only {len(fg - fb)}")

    # 3. drift
    (bb, bgnd), (gb, ggnd) = b["drift"], g["drift"]
    cut, rate = matched_cutoff(bb, BGE_CUTOFFS["drift"], gb)
    print(f"\nDRIFT: {rate:.1%} of random unrelated pairs pass 0.5 under bge -> gemma cutoff {cut:.3f}")
    print(f"  random-pair quartiles bge {np.round(np.quantile(bb, [.5, .9, .99]), 3)}"
          f"  gemma {np.round(np.quantile(gb, [.5, .9, .99]), 3)}")
    gmap = dict(ggnd)
    for rel, s in bgnd:
        flag_b = "FLAG" if s < 0.5 else "ok"
        flag_g = "FLAG" if gmap.get(rel, 1) < cut else "ok"
        print(f"    artifact {rel[-60:]:<60} bge {s:.3f} {flag_b:<4}  gemma {gmap.get(rel, float('nan')):.3f} {flag_g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
