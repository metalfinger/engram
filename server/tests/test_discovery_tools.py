"""M5.5 — discovery end to end: publish -> discover -> read public -> follow -> feed -> ask -> answer.

The security cases matter most here: private work must never surface through ANY public
path, and a question must never touch the owner's git brain."""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.errors import KBError
from engram_server.registry import StoreRegistry


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
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "google", f"google:{e}")
    return registry


def _login(monkeypatch, email):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


def _doc(desc, body="Body text.", vis=None):
    v = f"visibility: {vis}\n" if vis else ""
    return f"---\ntype: decision\ndescription: {desc}\n{v}---\n\n# T\n\n{body}\n"


async def _alice_publishes(monkeypatch):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_write("projects/openwork/context.md", _doc("an open project"), "seed")
    await app_module.kb_write("projects/openwork/pub.md", _doc("published thing", "The public answer is 42."), "seed")
    await app_module.kb_write("projects/openwork/secret.md", _doc("private thing", "SECRETSAUCE"), "seed")
    await app_module.kb_publish("projects/openwork/pub.md", "public")


# -- publish + audit --------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_then_audit(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    audit = await app_module.kb_public()
    paths = {i["path"] for i in audit["public"]}
    assert "projects/openwork/pub.md" in paths
    assert "projects/openwork/secret.md" not in paths


# -- discovery + public read ------------------------------------------------


@pytest.mark.asyncio
async def test_bob_discovers_and_reads_alices_public_work(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")

    directory = await app_module.kb_explore()
    assert any(p["handle"] == "alice" for p in directory["people"])

    profile = await app_module.kb_explore(handle="alice")
    assert profile["profile"]["handle"] == "alice"
    assert any(w["path"] == "projects/openwork/pub.md" for w in profile["public_work"])
    assert all("secret" not in w["path"] for w in profile["public_work"])

    got = await app_module.kb_read_public("alice", "projects/openwork/pub.md")
    assert "public answer is 42" in got["content"]


@pytest.mark.asyncio
async def test_private_work_is_never_readable_publicly(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(KBError, match="not public"):
        await app_module.kb_read_public("alice", "projects/openwork/secret.md")


@pytest.mark.asyncio
async def test_search_public_only_finds_published(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")
    hits = await app_module.kb_explore(query="published thing")
    assert any(h["path"] == "projects/openwork/pub.md" for h in hits["results"])
    assert not (await app_module.kb_explore(query="private thing"))["results"]


# -- follow + feed ----------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_is_one_directional_and_feeds(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")

    assert (await app_module.kb_follow("alice"))["following"] is True
    feed = await app_module.kb_feed()
    assert any(i["path"] == "projects/openwork/pub.md" for i in feed["items"])

    # alice does NOT automatically follow bob back — that's the contacts model, not follow
    _login(monkeypatch, "alice@example.com")
    assert not (await app_module.kb_feed())["items"]

    _login(monkeypatch, "bob@example.com")
    assert (await app_module.kb_follow("alice", unfollow=True))["following"] is False
    assert not (await app_module.kb_feed())["items"]


# -- ask + answer -----------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_and_answer_round_trip(mu, monkeypatch):
    await _alice_publishes(monkeypatch)

    _login(monkeypatch, "bob@example.com")
    asked = await app_module.kb_ask("alice", "projects/openwork/pub.md", "Why 42?")
    assert asked["to"] == "alice"

    # alice sees it awaiting an answer
    _login(monkeypatch, "alice@example.com")
    mine = await app_module.kb_asks()
    assert any(a["question"] == "Why 42?" and a["from"] == "bob" for a in mine["to_answer"])
    await app_module.kb_answer(asked["ask_id"], "Because it is the answer.")

    # bob sees the answer
    _login(monkeypatch, "bob@example.com")
    back = await app_module.kb_asks()
    answered = [a for a in back["i_asked"] if a["id"] == asked["ask_id"]]
    assert answered and answered[0]["answer"] == "Because it is the answer."
    assert answered[0]["status"] == "answered"


@pytest.mark.asyncio
async def test_question_never_touches_the_owners_brain(mu, monkeypatch):
    """The quarantine guarantee: a stranger's text is not committed into alice's git."""
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")
    await app_module.kb_ask("alice", "projects/openwork/pub.md", "UNIQUEQUESTIONTOKEN?")

    alice_store = await mu.store_for_handle("alice")
    hits = [p for p in alice_store.root.rglob("*.md")
            if "UNIQUEQUESTIONTOKEN" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [], "a question must never be written into the owner's brain"


@pytest.mark.asyncio
async def test_cannot_ask_about_private_work(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(KBError, match="not public"):
        await app_module.kb_ask("alice", "projects/openwork/secret.md", "what is this?")


@pytest.mark.asyncio
async def test_only_the_owner_can_answer(mu, monkeypatch):
    await _alice_publishes(monkeypatch)
    _login(monkeypatch, "bob@example.com")
    asked = await app_module.kb_ask("alice", "projects/openwork/pub.md", "Why?")
    # bob (the asker, not the owner) may not answer his own question
    with pytest.raises(KBError):
        await app_module.kb_answer(asked["ask_id"], "I'll answer myself")
