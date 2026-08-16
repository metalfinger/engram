"""kb_realign — the one-word orientation verb, made native.

The protocol lives in library/runbooks/realign.md; these tests pin the parts a
session must not get wrong: resolution ORDER (named -> learned route -> guess ->
ask), the routing table DERIVING itself from presence history rather than being
maintained by hand, and the refusal to silently file work under a neighbour.
"""

import pytest

from engram_server.kbstore import KBStore


@pytest.fixture()
async def store(settings):
    s = KBStore(settings)
    await s.start()
    return s


def _project(desc: str, phase: str = "Shipping.", loops: str = "- [ ] the top loop") -> str:
    return (
        f"---\ntype: project\ndescription: {desc}\n---\n\n"
        f"# About\n\n{desc}\n\n"
        f"# Current Phase\n\n{phase}\n\n"
        f"# Open Loops\n\n{loops}\n\n"
        f"# Next Actions\n\n- [ ] do the thing\n"
    )


async def _seed(store, *ids):
    for pid in ids:
        await store.kb_write(f"projects/{pid}/context.md", _project(f"{pid} project"), "seed")


# -- resolution order --------------------------------------------------------


@pytest.mark.asyncio
async def test_named_project_wins(store):
    await _seed(store, "engram", "slate")
    out = await store.kb_realign(project="slate")
    assert out["resolved"] and out["project"] == "slate"
    assert out["resolved_by"] == "named"


@pytest.mark.asyncio
async def test_loose_name_match(store):
    """'vibechk' should find vibechk-brand — the user says a word, not an id."""
    await _seed(store, "vibechk-brand", "engram")
    out = await store.kb_realign(project="vibechk")
    assert out["resolved"] and out["project"] == "vibechk-brand"


@pytest.mark.asyncio
async def test_ambiguous_name_asks_instead_of_picking(store):
    await _seed(store, "vibechk-brand", "vibechk-mcp-ux")
    out = await store.kb_realign(project="vibechk")
    assert out["resolved"] is False
    assert {p["id"] for p in out["candidates"]} == {"vibechk-brand", "vibechk-mcp-ux"}
    assert "ONE question" in out["instruction"]


@pytest.mark.asyncio
async def test_unresolved_never_files_under_a_neighbour(store):
    await _seed(store, "engram")
    out = await store.kb_realign(cwd="D:/somewhere/unrelated")
    assert out["resolved"] is False
    assert out["candidates"]
    assert "no project yet" in out["instruction"]
    # and nothing was created for the unmatched location
    assert "unrelated" not in {p["id"] for p in await store.kb_projects()}


# -- the routing table, derived ----------------------------------------------


@pytest.mark.asyncio
async def test_route_learned_from_presence_repo_remote(store):
    """No table to maintain: a session that attached to a project from a repo has
    already taught the route."""
    await _seed(store, "pixelpuri", "engram")
    await store.kb_presence(
        session="pc1-cc", project="pixelpuri",
        repo_remote="git@github.com:metalfinger/pixelpuri.git",
        cwd="D:/work/pixelpuri",
    )
    out = await store.kb_realign(repo="https://github.com/metalfinger/pixelpuri")
    assert out["resolved"] and out["project"] == "pixelpuri"
    assert "learned route" in out["resolved_by"]
    assert out["guess"] is False


@pytest.mark.asyncio
async def test_route_matches_a_subdirectory_of_a_known_cwd(store):
    await _seed(store, "engram")
    await store.kb_presence(session="pc1", project="engram", cwd="C:/code/engram")
    out = await store.kb_realign(cwd="C:/code/engram/server/engram_server")
    assert out["resolved"] and out["project"] == "engram"
    assert "learned route" in out["resolved_by"]


@pytest.mark.asyncio
async def test_route_table_self_maintains_when_a_repo_changes_project(store):
    """Newest presence record wins — a retired project stops capturing its old repo."""
    await _seed(store, "old-thing", "new-thing")
    await store.kb_presence(session="s1", project="old-thing", cwd="C:/code/shared")
    await store.kb_presence(session="s2", project="new-thing", cwd="C:/code/shared")
    out = await store.kb_realign(cwd="C:/code/shared")
    assert out["project"] == "new-thing"


@pytest.mark.asyncio
async def test_directory_name_guess_is_flagged(store):
    """Weakest signal, still usually right — but the caller must confirm it."""
    await _seed(store, "stellar-ux-exploration", "slate")
    out = await store.kb_realign(cwd="D:/Projects/slate")
    assert out["resolved"] and out["project"] == "slate"
    assert out["guess"] is True
    assert "guess" in out["resolved_by"]


# -- the orientation block ---------------------------------------------------


@pytest.mark.asyncio
async def test_orientation_block_is_compact_and_complete(store):
    await store.kb_write(
        "projects/engram/context.md",
        _project("engram project", phase="v3 shipped.", loops="- [ ] seed decisions"),
        "seed",
    )
    await store.kb_append_log("engram", "shipped rooms")
    await store.kb_leave_message("engram", "Read this first", "Do the thing.", to="any")
    out = await store.kb_realign(project="engram")

    assert out["phase"] == "v3 shipped."
    assert "seed decisions" in out["open_loops"]
    assert "do the thing" in out["next_actions"].lower()
    assert out["context_path"] == "projects/engram/context.md"
    assert out["pin_file"] == ".engram-project" and out["pin_content"] == "engram"
    assert len(out["unread_messages"]) == 1
    assert out["last_session"] is not None
    # the instruction carries the protocol a skill-less client needs
    assert "FIRST" in out["instruction"] and "ONE line" in out["instruction"]
    assert "drift" in out["instruction"].lower()


@pytest.mark.asyncio
async def test_sequence_prefers_a_backlog_file_over_context_loops(store):
    await _seed(store, "engram")
    await store.kb_write(
        "projects/engram/backlog.md",
        "---\ntype: note\ndescription: what's next\n---\n\n# Backlog\n\n1. ship it\n",
        "seed",
    )
    out = await store.kb_realign(project="engram")
    assert out["sequence_path"] == "projects/engram/backlog.md"
    assert "ship it" in out["sequence"]


@pytest.mark.asyncio
async def test_sequence_falls_back_to_open_loops(store):
    await _seed(store, "engram")
    out = await store.kb_realign(project="engram")
    assert out["sequence_path"] == "projects/engram/context.md"
    assert "the top loop" in out["sequence"]


@pytest.mark.asyncio
async def test_works_for_a_foldered_project(store):
    """Project-agnostic — and folders must not break resolution."""
    await _seed(store, "engram")
    await store.kb_move_project("engram", "personal")
    out = await store.kb_realign(project="engram")
    assert out["resolved"] and out["project"] == "engram"
    assert out["folder"] == "personal"
    assert out["context_path"] == "projects/personal/engram/context.md"


@pytest.mark.asyncio
async def test_map_is_noted_not_read(store):
    await _seed(store, "vibechk")
    await store.kb_write(
        "projects/vibechk/map.md",
        "---\ntype: note\ndescription: topic routing\n---\n\n# Map\n\nStuff.\n",
        "seed",
    )
    out = await store.kb_realign(project="vibechk")
    assert out["map_path"] == "projects/vibechk/map.md"


# -- found by dogfooding: sessions live in SUBDIRECTORIES ---------------------


@pytest.mark.asyncio
async def test_guess_walks_up_from_a_subdirectory(store):
    """The first live call failed on engram's OWN repo: cwd was `<repo>/server`,
    and a guess that only looked at the last path segment saw 'server'. Sessions
    sit in subdirectories far more often than at the repo root."""
    await _seed(store, "engram")
    out = await store.kb_realign(cwd="C:/Users/Admin/Documents/code/engram/server")
    assert out["resolved"] and out["project"] == "engram"
    assert out["guess"] is True


@pytest.mark.asyncio
async def test_guess_prefers_the_nearest_matching_directory(store):
    """Walking up must stop at the FIRST match — the innermost directory that
    names a project is the more specific answer."""
    await _seed(store, "engram", "code")
    out = await store.kb_realign(cwd="D:/code/engram/server/engram_server")
    assert out["project"] == "engram"


@pytest.mark.asyncio
async def test_candidates_are_a_shortlist_not_the_whole_brain(store):
    """The instruction says 'never make the user read the whole list', so the
    payload must not BE the whole list — ranked by recency, capped."""
    await _seed(store, *[f"proj-{i}" for i in range(12)])
    out = await store.kb_realign(cwd="D:/nowhere/unmatched")
    assert out["resolved"] is False
    assert 0 < len(out["candidates"]) <= 8


@pytest.mark.asyncio
async def test_repo_name_unrelated_to_project_name_does_not_guess_wrong(store):
    """Requested by the session that asked for realign, from its own live case:
    repo `formthefuture-v2` belongs to project `vibechk`. Nothing in the path
    resembles the project, so the guess rung MUST stay silent rather than be
    confidently wrong — resolution falls to the ask, with a usable shortlist."""
    await _seed(store, "vibechk", "vibechk-brand", "engram")
    out = await store.kb_realign(
        repo="https://github.com/Alt-AI-Inc/formthefuture-v2.git",
        cwd="/Users/hirenk/Documents/code/alt.inc/formthefuture-v2",
    )
    # The point: NO confidently-wrong answer. Nothing in the path resembles
    # any project, so the guess rung stays silent and we fall to the ask.
    assert out["resolved"] is False
    assert out.get("guess") in (None, False)
    assert out["candidates"], "an ask still needs a shortlist to offer from"


@pytest.mark.asyncio
async def test_a_pin_teaches_the_route_that_repo_naming_cannot(store):
    """...and the fix for the case above: once ONE session attaches there, the
    learned route carries it forever — no naming coincidence required."""
    await _seed(store, "vibechk")
    await store.kb_presence(
        session="mac-cc", project="vibechk",
        repo_remote="https://github.com/Alt-AI-Inc/formthefuture-v2.git",
        cwd="/Users/hirenk/Documents/code/alt.inc/formthefuture-v2",
    )
    out = await store.kb_realign(
        repo="git@github.com:Alt-AI-Inc/formthefuture-v2.git",  # different form, same remote
        cwd="/Users/hirenk/Documents/code/alt.inc/formthefuture-v2",
    )
    assert out["resolved"] and out["project"] == "vibechk"
    assert "learned route" in out["resolved_by"]


@pytest.mark.asyncio
async def test_candidates_are_lean(store):
    """Payload discipline: enough to name one candidate, nothing more."""
    await _seed(store, "a-proj", "b-proj")
    out = await store.kb_realign(cwd="D:/no/match/here")
    assert set(out["candidates"][0]) == {
        "id", "title", "description", "folder", "last_session"
    }


@pytest.mark.asyncio
async def test_shortlist_floats_the_relevant_project_over_the_recent_one(store):
    """Relevance beats recency: a busy unrelated project must not crowd out the
    one whose name is sitting in the path."""
    await _seed(store, "pixelpuri", "slate")
    await store.kb_append_log("slate", "worked on slate today")  # slate is 'recent'
    out = await store.kb_realign(cwd="D:/work/pixelpuri-dataset/scripts")
    ids = [p["id"] for p in out["candidates"]]
    assert ids[0] == "pixelpuri", ids


@pytest.mark.asyncio
async def test_generic_path_words_do_not_score(store):
    """`.../Documents/code/server/...` must not make a project called 'server'
    or 'code' look relevant — those words describe every filesystem."""
    await _seed(store, "server", "engram")
    out = await store.kb_realign(cwd="C:/Users/Admin/Documents/code/unmatched-thing")
    top = out["candidates"][0]["id"]
    assert top != "server", out["candidates"]


@pytest.mark.asyncio
async def test_a_rare_name_match_beats_two_common_org_words(store):
    """Live finding: `.../alt.inc/formthefuture-v2` floated three unrelated 'Alt
    Inc' projects above the one whose NAME was in the path, because raw hit
    counting let two generic words outvote one distinctive match."""
    common = "shared work at Alt Inc"
    for pid in ("stellar-ux", "human-ai", "eod-update"):
        await store.kb_write(f"projects/{pid}/context.md", _project(common), "seed")
    await store.kb_write(
        "projects/formthefuture-ux/context.md",
        _project("Respondent experience for FormTheFuture"), "seed",
    )
    out = await store.kb_realign(cwd="/Users/hirenk/code/alt.inc/formthefuture-v2")
    assert out["candidates"][0]["id"] == "formthefuture-ux", [
        c["id"] for c in out["candidates"]
    ]


# -- the pin rung (a declaration outranks an inference) ----------------------


@pytest.mark.asyncio
async def test_pin_resolves_cold_with_no_history(store):
    """`.engram-project` commits with the repo, so it works on a machine that has
    never run a session — the case the learned table cannot serve."""
    await _seed(store, "vibechk", "engram")
    out = await store.kb_realign(
        pin="vibechk",
        repo="https://github.com/Alt-AI-Inc/formthefuture-v2.git",
        cwd="/Users/hirenk/code/alt.inc/formthefuture-v2",
    )
    assert out["resolved"] and out["project"] == "vibechk"
    assert out["resolved_by"] == "pin (.engram-project)"
    assert out["guess"] is False


@pytest.mark.asyncio
async def test_pin_outranks_a_stale_learned_route(store):
    """A deliberate declaration beats an inference from history."""
    await _seed(store, "old-thing", "new-thing")
    await store.kb_presence(session="s1", project="old-thing", cwd="C:/code/shared")
    out = await store.kb_realign(pin="new-thing", cwd="C:/code/shared")
    assert out["project"] == "new-thing"


@pytest.mark.asyncio
async def test_a_bogus_pin_falls_through_rather_than_failing(store):
    """A pin naming a project that no longer exists must not dead-end the call."""
    await _seed(store, "engram")
    await store.kb_presence(session="s1", project="engram", cwd="C:/code/engram")
    out = await store.kb_realign(pin="deleted-project", cwd="C:/code/engram")
    assert out["resolved"] and out["project"] == "engram"


# -- silence must be explainable --------------------------------------------


@pytest.mark.asyncio
async def test_empty_routing_table_says_it_is_unwired(store):
    """An empty table and a table with no match look identical to a caller — and
    the first is a setup problem, not a resolution failure. Cost a session 20
    minutes of diagnosis once."""
    await _seed(store, "engram")
    out = await store.kb_realign(cwd="D:/somewhere/unknown")
    assert out["routing"]["routes_known"] == 0
    assert "hooks" in out["routing"]["diagnosis"]


@pytest.mark.asyncio
async def test_unpinned_repos_are_diagnosed_differently_from_missing_hooks(store):
    """THE root cause, found live: hooks installed and faithfully recording
    repo/cwd, but every record has project:'' because the repo was never pinned.
    That is a different fix from 'install the hooks' and must say so."""
    await _seed(store, "engram")
    await store.kb_presence(session="pc1", cwd="C:/code/engram",
                            repo_remote="https://github.com/x/engram.git")  # no project
    out = await store.kb_realign(cwd="D:/elsewhere/unknown")
    assert out["routing"]["routes_known"] == 0
    assert "PINNED" in out["routing"]["diagnosis"]
    assert "hooks are probably not" not in out["routing"]["diagnosis"]


@pytest.mark.asyncio
async def test_missing_headings_are_named_not_silently_empty(store):
    """An absent '## Open loops' must not read as 'nothing outstanding'."""
    await store.kb_write(
        "projects/bare/context.md",
        "---\ntype: project\ndescription: bare\n---\n\n# About\n\nNothing structured.\n",
        "seed",
    )
    out = await store.kb_realign(project="bare")
    assert out["open_loops"] == ""
    assert set(out["missing_sections"]) == {"current phase", "open loops", "next actions"}


@pytest.mark.asyncio
async def test_a_cut_sequence_announces_itself(store):
    await _seed(store, "big")
    await store.kb_write(
        "projects/big/backlog.md",
        "---\ntype: note\ndescription: long\n---\n\n# Backlog\n\n"
        + "\n".join(f"- item number {i} with enough words to add length" for i in range(200)),
        "seed",
    )
    out = await store.kb_realign(project="big")
    assert out["sequence_truncated"] is True
    assert out["sequence"].rstrip().endswith("]")
    assert not out["sequence"].rstrip().endswith("leng")  # never mid-word


# -- proof, not assertion ----------------------------------------------------


@pytest.mark.asyncio
async def test_log_entries_can_carry_commit_proof(store):
    """A log saying 'shipped X' is a claim; a commit link is evidence."""
    await _seed(store, "engram")
    res = await store.kb_append_log(
        "engram", "shipped the realign verb",
        commits=["b36a9a4c", "https://github.com/metalfinger/engram/commit/f4c37af0"],
        repo="metalfinger/engram",
    )
    assert "warnings" not in res
    log = (store.root / "projects/engram/log.md").read_text(encoding="utf-8")
    assert "https://github.com/metalfinger/engram/commit/b36a9a4c" in log
    assert "[f4c37af0]" in log


@pytest.mark.asyncio
async def test_a_shipping_claim_without_proof_is_nudged(store):
    await _seed(store, "engram")
    res = await store.kb_append_log("engram", "shipped the whole thing today")
    assert any("proof" in w.lower() for w in res.get("warnings", []))
    # a plain narrative entry isn't nagged
    quiet = await store.kb_append_log("engram", "explored options, decided nothing yet")
    assert "warnings" not in quiet


@pytest.mark.asyncio
async def test_bad_commit_refs_are_dropped_not_rendered(store):
    await _seed(store, "engram")
    await store.kb_append_log(
        "engram", "shipped it", commits=["not-a-sha", "deadbeef"], repo="metalfinger/engram"
    )
    log = (store.root / "projects/engram/log.md").read_text(encoding="utf-8")
    assert "deadbeef" in log and "not-a-sha" not in log


@pytest.mark.asyncio
async def test_a_weakly_resolved_repo_is_nagged_to_pin_itself(store):
    """Their closing suggestion, which follows from the root cause: nag about
    PINS, not hooks. A pin is per-repo, it's the only thing that teaches the
    routing table, and the session is standing next to the repo when it resolves."""
    await _seed(store, "engram")
    out = await store.kb_realign(cwd="C:/code/engram/server")
    assert out["resolved"] and out["guess"] is True
    assert "engram-project" in out["pin_nudge"]
    assert "invisible to routing" in out["pin_nudge"]


@pytest.mark.asyncio
async def test_an_already_pinned_repo_is_not_nagged(store):
    await _seed(store, "engram")
    out = await store.kb_realign(pin="engram", cwd="C:/code/engram")
    assert out["resolved_by"].startswith("pin")
    assert out["pin_nudge"] == ""


@pytest.mark.asyncio
async def test_a_recently_worked_project_outranks_a_never_logged_one(store):
    """Regression, found live: the no-log tiebreak was INVERTED, so brand-new
    projects ranked above ones worked on today and pushed an active hub off the
    shortlist entirely."""
    await _seed(store, "worked-today", "never-touched")
    await store.kb_append_log("worked-today", "did the thing")
    out = await store.kb_realign(cwd="D:/no/relevance/here")
    ids = [c["id"] for c in out["candidates"]]
    # Recent work makes the shortlist; a never-logged project does not displace it.
    assert "worked-today" in ids, ids
    assert ids.index("worked-today") < len(ids)
    if "never-touched" in ids:
        assert ids.index("worked-today") < ids.index("never-touched"), ids


@pytest.mark.asyncio
async def test_a_folder_hub_is_offered_alongside_its_siblings(store):
    """The failure mode that matters: seven vibechk-suite siblings were offered
    and the hub itself — the correct answer — was missing. A hub's map routes
    onward, so if a sibling looks relevant the hub is at least as good."""
    await _seed(store, "vibechk", "vibechk-sprite-pipeline", "formthefuture-ux")
    for pid in ("vibechk", "vibechk-sprite-pipeline", "formthefuture-ux"):
        await store.kb_move_project(pid, "vibechk-suite")
    out = await store.kb_realign(
        repo="https://github.com/Alt-AI-Inc/formthefuture-v2.git",
        cwd="/Users/hirenk/Documents/code/alt.inc/formthefuture-v2",
    )
    ids = [c["id"] for c in out["candidates"]]
    # the sibling that matched AND the rest of its family, hub included
    assert "formthefuture-ux" in ids, ids
    assert "vibechk" in ids, ids
