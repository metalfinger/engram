"""v3 Wave 4 — rooms end to end at the TOOL layer: open -> invite -> post -> read ->
grant -> cross-brain search/fetch -> close-with-precipitate.

The security cases matter most: a grant only exposes its prefix (never the rest of the
brain), every guest access lands in the transcript as an audit turn, grants die with
the room, secrets never enter a room, and the close outcome is OFFERED — the tool
returns an instruction, it never writes into anyone's brain.
"""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.errors import KBError
from engram_server.registry import StoreRegistry

AWS = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    s = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
    })
    registry = StoreRegistry(s)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", s)
    # presence throttle is process-global; each test gets a fresh event loop whose
    # clock restarts, so stale entries would suppress touches for a brand-new DB.
    monkeypatch.setattr(app_module, "_presence_last", {})
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    return registry


def _login(monkeypatch, email):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


def _doc(desc, body="Body text."):
    return f"---\ntype: decision\ndescription: {desc}\n---\n\n# T\n\n{body}\n"


def _uid(registry, handle):
    return registry.tenancy.user_by_handle(handle).id


# -- open / invite / post / read -------------------------------------------


@pytest.mark.asyncio
async def test_room_flow_open_invite_post_read(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    opened = await app_module.kb_room_open(
        "Deploy-Fix", "unblock the deploy", exit_condition="a decision either way",
        invite="@bob", turn_budget=10,
    )
    assert opened["room"]["name"] == "deploy-fix"  # slug lowercased
    assert opened["invited"] == ["bob"]
    # bob got a real notification with the room name as ref
    notes = mu.social.list_notifications(_uid(mu, "bob"), unread_only=True)
    assert any(n.kind == "room_invite" and n.ref == "deploy-fix" for n in notes)

    _login(monkeypatch, "bob@example.com")
    posted = await app_module.kb_room_post("deploy-fix", "the pipeline dies at step 3")
    assert posted["turn"]["author"] == "bob"

    _login(monkeypatch, "alice@example.com")
    got = await app_module.kb_room_read("deploy-fix", since=0)
    bodies = [t["body"] for t in got["turns"]]
    assert any("step 3" in b for b in bodies)
    assert got["room"]["goal"] == "unblock the deploy"
    # the opening system turn is visible too
    assert any(t["kind"] == "system" for t in got["turns"])


@pytest.mark.asyncio
async def test_wait_for_reply_times_out_empty(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_room_open("solo-room", "waiting test", invite="@bob")
    res = await app_module.kb_room_post(
        "solo-room", "anyone there?", wait_for_reply=True, wait_seconds=1
    )
    assert res["replies"] == []  # nobody answered; long-poll lapsed quietly


@pytest.mark.asyncio
async def test_non_member_cannot_read_or_post(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_room_open("closed-club", "members only")
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_room_read("closed-club")
    with pytest.raises(KBError):
        await app_module.kb_room_post("closed-club", "let me in")


@pytest.mark.asyncio
async def test_budget_refusal_teaches_extend(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_room_open("tight", "small budget", turn_budget=2, hard_cap=10)
    await app_module.kb_room_post("tight", "one")
    await app_module.kb_room_post("tight", "two")
    with pytest.raises(KBError, match="extend"):
        await app_module.kb_room_post("tight", "three")
    await app_module.kb_room_extend("tight", extra_turns=5)
    assert (await app_module.kb_room_post("tight", "three"))["turn"]["body"] == "three"


# -- secrets never enter a room --------------------------------------------


@pytest.mark.asyncio
async def test_secret_scan_refuses_post_and_outcome(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_room_open("sec", "secret handling")
    with pytest.raises(KBError, match="secret"):
        await app_module.kb_room_post("sec", f"creds are {AWS}")
    with pytest.raises(KBError, match="secret"):
        await app_module.kb_room_close("sec", outcome=f"use {AWS}")


# -- room-scoped grants: the live join -------------------------------------


async def _alice_slate(monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_write("projects/slate/context.md", _doc("slate project"), "seed")
    await app_module.kb_write(
        "projects/slate/decisions/render.md",
        _doc("render pipeline decision", "We render via comfy queue batching."), "seed",
    )
    await app_module.kb_write(
        "projects/private-x/context.md", _doc("other private project", "Hidden."), "seed",
    )


@pytest.mark.asyncio
async def test_grant_scopes_search_and_fetch_with_audit(mu, monkeypatch):
    await _alice_slate(monkeypatch)
    await app_module.kb_room_open("slate-help", "debug rendering", invite="@bob",
                                  grant="projects/slate")

    _login(monkeypatch, "bob@example.com")
    found = await app_module.kb_room_search("slate-help", "alice", "render pipeline")
    assert found["results"], "granted work should be searchable"
    assert all(r["path"].startswith("projects/slate") for r in found["results"])

    got = await app_module.kb_room_fetch("slate-help", "alice",
                                         "projects/slate/decisions/render.md")
    assert "comfy queue" in got["content"]
    assert got["owner"] == "alice"

    # the un-granted project is invisible to fetch
    with pytest.raises(Exception):
        await app_module.kb_room_fetch("slate-help", "alice", "projects/private-x/context.md")

    # every guest access is an audit turn in the transcript
    _login(monkeypatch, "alice@example.com")
    turns = (await app_module.kb_room_read("slate-help", since=0))["turns"]
    audits = [t for t in turns if t["kind"] == "guest_read"]
    assert len(audits) >= 2  # one search + one fetch
    assert any("searched" in t["body"] for t in audits)
    assert any("read" in t["body"] for t in audits)


@pytest.mark.asyncio
async def test_grants_die_with_the_room(mu, monkeypatch):
    await _alice_slate(monkeypatch)
    await app_module.kb_room_open("ephemeral", "short-lived access", invite="@bob",
                                  grant="projects/slate")
    await app_module.kb_room_close("ephemeral", outcome="done")
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(Exception):
        await app_module.kb_room_fetch("ephemeral", "alice",
                                       "projects/slate/decisions/render.md")


# -- close: the precipitate is OFFERED, never written -----------------------


@pytest.mark.asyncio
async def test_close_offers_precipitate_and_notifies(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_room_open("decide", "pick a queue system", invite="@bob")
    _login(monkeypatch, "bob@example.com")
    await app_module.kb_room_post("decide", "redis streams is enough")
    closed = await app_module.kb_room_close("decide", outcome="Use redis streams; revisit at 10x volume.")
    assert closed["room"]["status"] == "closed"
    assert "kb_write" in closed["precipitate_instruction"]
    assert "yes" in closed["precipitate_instruction"]  # explicit-consent wording
    # alice (the OTHER member) got the close notification
    notes = mu.social.list_notifications(_uid(mu, "alice"), unread_only=True)
    assert any(n.kind == "room_closed" for n in notes)
    # nothing was written into either brain by the close itself
    _login(monkeypatch, "alice@example.com")
    pub = await app_module.kb_public()
    assert all("decide" not in p["path"] for p in pub["public"] + pub["contacts"])


# -- team presence ----------------------------------------------------------


@pytest.mark.asyncio
async def test_kb_team_roster_and_invisible(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_write("projects/p1/context.md", _doc("p"), "seed")  # touches presence
    _login(monkeypatch, "bob@example.com")
    team = await app_module.kb_team()
    assert any(m["handle"] == "alice" for m in team["team"])
    # bob goes invisible: vanishes from alice's roster but sees his own state
    mine = await app_module.kb_team(invisible=True)
    assert mine["me"]["invisible"] is True
    _login(monkeypatch, "alice@example.com")
    team2 = await app_module.kb_team()
    assert all(m["handle"] != "bob" for m in team2["team"])


# -- sec-review hardening (v3 assessment) -----------------------------------


@pytest.mark.asyncio
async def test_non_member_cannot_search_or_fetch_granted_content(mu, monkeypatch):
    """Membership must be checked EXPLICITLY, not via the audit-write side effect."""
    await _alice_slate(monkeypatch)
    await app_module.kb_room_open("private-room", "granted access", grant="projects/slate")
    _login(monkeypatch, "bob@example.com")  # bob was never invited
    with pytest.raises(KBError, match="not a member"):
        await app_module.kb_room_search("private-room", "alice", "render")
    with pytest.raises(KBError, match="not a member"):
        await app_module.kb_room_fetch("private-room", "alice",
                                       "projects/slate/decisions/render.md")


@pytest.mark.asyncio
async def test_grant_check_rejects_traversal_paths_by_itself(mu, monkeypatch):
    """assert_grant must refuse '..' textual-prefix tricks WITHOUT relying on
    kbstore's independent path validation (self-sufficient boundary)."""
    from engram_server.teamwork import TeamworkError

    await _alice_slate(monkeypatch)
    await app_module.kb_room_open("trav", "traversal test", invite="@bob",
                                  grant="projects/slate")
    r = app_module.registry.rooms.room_by_name("trav")
    alice_id = _uid(mu, "alice")
    with pytest.raises(TeamworkError, match="'\.'"):
        app_module.registry.rooms.assert_grant(
            r.id, alice_id, "projects/slate/../private-x/context.md"
        )


@pytest.mark.asyncio
async def test_turns_carry_who_actually_wrote_them(mu, monkeypatch):
    """Demarcation (Hiren): a turn from the person and a turn from their Claude
    must be distinguishable. MCP posts -> via=claude; the widget composer ->
    via=human; system turns carry no via."""
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_room_open("who-said-it", "actor attribution", invite="@bob")
    await app_module.kb_room_post("who-said-it", "my Claude wrote this")
    await app_module.room_reply("who-said-it", "the human typed this in the app")
    turns = (await app_module.kb_room_read("who-said-it", since=0))["turns"]
    by_body = {t["body"]: t for t in turns}
    assert by_body["my Claude wrote this"]["via"] == "claude"
    assert by_body["the human typed this in the app"]["via"] == "human"
    assert "via" not in next(t for t in turns if t["kind"] == "system")


@pytest.mark.asyncio
async def test_turns_carry_refs_so_documents_travel_as_pointers(mu, monkeypatch):
    """Share, don't paste: a substantial thing belongs in the brain and the room
    carries its path. Refs survive the round trip and legacy turns have none."""
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_write("projects/ref/context.md", _doc("ref project"), "seed")
    await app_module.kb_room_open("refs-room", "share a design", invite="@bob")
    await app_module.kb_room_post(
        "refs-room", "Design is written up — see the ref, summary: we picked sqlite.",
        refs=["projects/ref/context.md"],
    )
    turns = (await app_module.kb_room_read("refs-room", since=0))["turns"]
    posted = next(t for t in turns if "picked sqlite" in t["body"])
    assert posted["refs"] == ["projects/ref/context.md"]
    # a turn with nothing attached doesn't carry an empty key
    system = next(t for t in turns if t["kind"] == "system")
    assert "refs" not in system
