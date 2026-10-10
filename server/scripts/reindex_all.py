"""reindex_all.py — re-embed EVERY account's brain into the configured collection.

For an embedding-model switch: the nightly reconcile only reindexes the owner's
brain, and other accounts' concepts are embedded only when written, so without
this their older concepts silently drop out of vector search in the new collection.

Safe beside the live server:
  - no git: stores are constructed but never start()ed, so this process never
    becomes a second writer on any brain checkout — it only reads files;
  - each store's own visibility resolver stamps every point, so public/private
    payloads (kb_explore, common_ground) come out right.

Usage (from server/, AFTER .env names the new model + collection):
    uv run --no-sync python scripts/reindex_all.py           # dry run: shows the plan
    uv run --no-sync python scripts/reindex_all.py --yes     # do it
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram_server.config import get_settings  # noqa: E402
from engram_server.kbstore import KBStore  # noqa: E402
from engram_server.provisioning import UserBrain, user_settings, users_root_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    targets = [(settings.owner_handle or "owner", settings)]
    root = users_root_path(settings)
    if settings.multiuser and root.exists():
        for d in sorted(p for p in root.iterdir() if (p / "brain").is_dir()):
            bare = d / "brain.git"
            brain = UserBrain(handle=d.name, bare=bare, checkout=d / "brain", remote=bare.as_uri())
            targets.append((d.name, user_settings(settings, brain)))

    print(f"model={settings.embed_model}\ncollection={settings.qdrant_collection}")
    for handle, s in targets:
        print(f"  @{handle}: {s.brain_path}  (tenant {s.tenant_id})")
    if not args.yes:
        print("\ndry run - pass --yes to reindex")
        return 0

    for handle, s in targets:
        store = KBStore(s)
        if store.semantic is None:
            print(f"@{handle}: semantic search is off (no qdrant_url?) - skipped")
            continue
        t = time.perf_counter()
        stats = store.semantic.full_reindex(store.root)
        print(f"@{handle}: {stats} in {time.perf_counter() - t:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
