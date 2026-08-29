"""Staleness surfacing — old, still-cited knowledge that nobody has confirmed.

The problem in the field's words: high-relevance memories become confidently
wrong after circumstances change. A June decision saying "blocked on the
invoice" is handed to a December session with the confidence it had the day it
was written.

THE RESET IS THE FEATURE. Detection is arithmetic; what keeps this from becoming
another always-red list is that confirming a concept makes it go quiet. Most of
these tests are about the going-quiet half.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engram_server.config import Settings
from engram_server.kbstore import KBStore
from engram_server.reconcile import _scan, _stale_candidates


NL = chr(10)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _old(days: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=days))


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _write(root, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _commit(root) -> None:
    """Fixtures must be COMMITTED before any write path runs: the store refuses
    to commit over a dirty checkout, and that refusal is a real guard worth
    keeping — the brain has exactly one writer, and a dirty tree means something
    else touched it."""
    import subprocess
    for args in (("add", "-A"), ("commit", "-m", "fixture", "--no-gpg-sign")):
        subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, encoding='utf-8')


def _live_project(root, pdir: str) -> None:
    """A project with recent activity — the liveness gate needs one, and a test
    that passed only because the gate silently excluded everything would be the
    always-red law in reverse."""
    ctx = (
        "---" + NL + "type: project" + NL + "title: p" + NL
        + "description: d" + NL + "status: active" + NL + "---" + NL + NL
        + "About." + NL
    )
    _write(root, pdir + "/context.md", ctx)
    _write(root, pdir + "/log.md",
           "# Log" + NL + NL + "## " + _old(1)[:10] + NL + NL + "* recent work" + NL)


def _concept(root, rel: str, *, type_="decision", days=400, extra="") -> None:
    _write(root, rel, (
        "---\n"
        f"type: {type_}\n"
        f"title: {rel}\n"
        "description: d\n"
        f"timestamp: '{_old(days)}'\n"
        f"{extra}"
        "---\n\nBody.\n"
    ))


def _cite(root, rel_from: str, target_basename: str) -> None:
    """A concept linking at another — staleness only applies to knowledge in USE."""
    _write(root, rel_from, (
        "---\ntype: note\ntitle: citer\ndescription: d\n"
        f"timestamp: '{_old(1)}'\n---\n\nSee [it]({target_basename}).\n"
    ))


@pytest.mark.asyncio
async def test_an_old_cited_decision_is_surfaced(store):
    root = store.root
    _live_project(root, "projects/p")
    _concept(root, "projects/p/decisions/old.md", days=400)
    _cite(root, "projects/p/decisions/citer.md", "old.md")
    scan = _scan(root)
    paths = [c["path"] for c in scan["stale"]]
    assert "projects/p/decisions/old.md" in paths
    entry = next(c for c in scan["stale"] if c["path"].endswith("old.md"))
    assert entry["age_days"] >= 400 and entry["links"] >= 1


@pytest.mark.asyncio
async def test_confirming_it_makes_it_quiet(store):
    """THE RESET. Without this the same items surface every night forever and the
    reader learns to skip the section — taking the real warnings with it."""
    root = store.root
    _live_project(root, "projects/p")
    _concept(root, "projects/p/decisions/old.md", days=400)
    _cite(root, "projects/p/decisions/citer.md", "old.md")
    assert any(c["path"].endswith("old.md") for c in _scan(root)["stale"])

    _commit(root)
    res = await store.kb_verify("projects/p/decisions/old.md")
    assert res["reviewed"]
    assert not any(c["path"].endswith("old.md") for c in _scan(root)["stale"]), (
        "a confirmed concept must stop being surfaced"
    )


@pytest.mark.asyncio
async def test_an_uncited_concept_is_an_orphan_not_a_staleness_case(store):
    """Different problem, different answer: reconcile already reports orphans.
    Staleness is about knowledge still being CONSULTED while possibly wrong."""
    root = store.root
    _live_project(root, "projects/p")
    _concept(root, "projects/p/decisions/lonely.md", days=999)
    assert not any(c["path"].endswith("lonely.md") for c in _scan(root)["stale"])


@pytest.mark.asyncio
async def test_recent_and_tentative_and_wrong_type_are_left_alone(store):
    root = store.root
    _live_project(root, "projects/p")
    _concept(root, "projects/p/decisions/fresh.md", days=10)
    _cite(root, "projects/p/decisions/c1.md", "fresh.md")
    _concept(root, "projects/p/decisions/unsure.md", days=400,
             extra="confidence: tentative\n")
    _cite(root, "projects/p/decisions/c2.md", "unsure.md")
    _concept(root, "projects/p/artifacts/thing.md", type_="artifact", days=999)
    _cite(root, "projects/p/artifacts/c3.md", "thing.md")

    paths = [c["path"] for c in _scan(root)["stale"]]
    assert not any(p.endswith("fresh.md") for p in paths), "not old enough"
    assert not any(p.endswith("unsure.md") for p in paths), "tentative reads provisional"
    assert not any(p.endswith("thing.md") for p in paths), "artifacts have own staleness"


@pytest.mark.asyncio
async def test_the_list_is_capped_and_ranked(store):
    """Unbounded lists are unread lists. Load-bearing AND old ranks first: a
    widely-cited stale decision misleads more sessions than a lonely one."""
    root = store.root
    _live_project(root, "projects/p")
    for i in range(12):
        _concept(root, f"projects/p/decisions/d{i}.md", days=300 + i)
        _cite(root, f"projects/p/decisions/c{i}.md", f"d{i}.md")
    stale = _scan(root)["stale"]
    assert len(stale) <= 7
    scores = [c["score"] for c in stale]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_search_and_read_carry_the_flag_at_retrieval(store):
    """The warning belongs where the risk is live — in the session about to act,
    not only in a nightly digest somebody has to remember to read."""
    root = store.root
    _live_project(root, "projects/p")
    _concept(root, "projects/p/decisions/invoice.md", days=400)
    _write(root, "projects/p/decisions/invoice.md", (
        "---\ntype: decision\ntitle: Invoice blocked\n"
        "description: the invoice is blocked\n"
        f"timestamp: '{_old(400)}'\n---\n\nThe invoice is blocked.\n"
    ))
    _commit(root)
    read = await store.kb_read("projects/p/decisions/invoice.md")
    assert read.get("verify") is True and read.get("age_days", 0) >= 400

    hits = await store.kb_search("invoice blocked", limit=5)
    hit = next((h for h in hits if h["path"].endswith("invoice.md")), None)
    if hit is not None:  # text scorer must find it; semantic may be absent in CI
        assert hit.get("verify") is True

    await store.kb_verify("projects/p/decisions/invoice.md")
    again = await store.kb_read("projects/p/decisions/invoice.md")
    assert "verify" not in again, "confirming clears the retrieval flag too"


@pytest.mark.asyncio
async def test_verifying_twice_in_a_day_is_a_no_change(store):
    """No empty commits: the brain has one writer and its history is read."""
    root = store.root
    _live_project(root, "projects/p")
    _concept(root, "projects/p/decisions/x.md", days=400)
    _commit(root)
    first = await store.kb_verify("projects/p/decisions/x.md")
    second = await store.kb_verify("projects/p/decisions/x.md")
    assert first["reviewed"] == second["reviewed"]
    assert second["no_change"] is True


@pytest.mark.asyncio
async def test_a_dormant_project_is_finished_not_stale(store):
    """Hiren's correction, and the design fix it produced: "it is just for things
    which are open right?" A dormant project's decisions are DONE, not rotting —
    flagging them is the noise that teaches a reader to skip the section.

    Measured on the real brain: stellar-ux-exploration (idle since 7 July) was
    surfacing under the old age-only rule and correctly drops out under this one.
    """
    root = store.root
    # Live project: recent log entry.
    _live_project(root, "projects/live")
    _concept(root, "projects/live/decisions/old.md", days=400)
    _cite(root, "projects/live/decisions/citer.md", "old.md")

    # Dormant project: same-age concept, same citation, but no recent activity.
    _write(root, "projects/dormant/context.md", (
        "---\ntype: project\ntitle: d\ndescription: d\nstatus: active\n---\n\nAbout.\n"
    ))
    _write(root, "projects/dormant/log.md",
           f"# Log\n\n## {_old(300)[:10]}\n\n* last touched ages ago\n")
    _concept(root, "projects/dormant/decisions/old.md", days=400)
    _cite(root, "projects/dormant/decisions/citer.md", "old.md")

    stale = _scan(root)["stale"]
    paths = [c["path"] for c in stale]
    assert any(p.startswith("projects/live/") for p in paths), "live work still flagged"
    assert not any(p.startswith("projects/dormant/") for p in paths), (
        "a dormant project's decisions are finished, not stale"
    )


@pytest.mark.asyncio
async def test_liveness_is_measured_not_declared(store):
    """Every project in the real brain says `status: active`, including ones
    untouched for months — a declared status is itself stale-prone, which is the
    exact problem being solved. So the log's newest entry decides, not the field.
    """
    root = store.root
    _write(root, "projects/claims-active/context.md", (
        "---\ntype: project\ntitle: c\ndescription: d\nstatus: active\n---\n\nAbout.\n"
    ))
    _write(root, "projects/claims-active/log.md",
           f"# Log\n\n## {_old(300)[:10]}\n\n* nothing since\n")
    _concept(root, "projects/claims-active/decisions/old.md", days=400)
    _cite(root, "projects/claims-active/decisions/citer.md", "old.md")

    assert not any(c["path"].startswith("projects/claims-active/")
                   for c in _scan(root)["stale"]), (
        "saying active must not be enough — the log decides"
    )
