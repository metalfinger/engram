"""M2.3 — the social MCP tools end-to-end: contacts, DMs, notifications, kb_load surfacing."""

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
    # two real accounts
    for handle, email in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(email)
        registry.tenancy.accept_invite(inv.token, handle, email, "google", f"google:{email}")
    return registry


def _login(monkeypatch, email):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


@pytest.mark.asyncio
async def test_contact_request_accept_then_dm_and_notify(mu, monkeypatch):
    # alice requests bob
    _login(monkeypatch, "alice@example.com")
    r = await app_module.kb_add_contact("bob")
    assert r["status"] == "pending"
    # bob sees the incoming request + a notification
    _login(monkeypatch, "bob@example.com")
    contacts = await app_module.kb_contacts()
    assert "alice" in contacts["incoming"]
    notes = await app_module.kb_notifications()
    assert any(n["kind"] == "contact_request" for n in notes["unread"])
    # bob accepts
    await app_module.kb_accept_contact("alice")
    # alice can now DM bob
    _login(monkeypatch, "alice@example.com")
    sent = await app_module.kb_dm("bob", "hey bob, ready for the review?")
    assert sent["sent"] and sent["to"] == "bob"
    # bob reads it + it's marked read
    _login(monkeypatch, "bob@example.com")
    convo = await app_module.kb_messages(with_handle="alice")
    assert convo["messages"][-1]["body"].startswith("hey bob")
    assert convo["messages"][-1]["from"] == "alice"
    dm_note = await app_module.kb_notifications()
    assert any(n["kind"] == "dm" for n in dm_note["unread"])


@pytest.mark.asyncio
async def test_notifications_limit_and_kind_filter(mu, monkeypatch):
    """The 2026-10-03 incident: a busy account's kb_notifications call came back
    at 90k+ chars because nothing capped it. `limit` bounds the row count and
    `kind` narrows to one kind — both needed once one kind (room_turn) can drown
    out everything else."""
    _login(monkeypatch, "alice@example.com")
    r = await app_module.kb_add_contact("bob")
    assert r["status"] == "pending"
    # bob already has one contact_request notification; add distinguishable ones
    bob_id = mu.tenancy.user_by_handle("bob").id
    for i in range(5):
        mu.social.create_notification(bob_id, "room_turn", f"turn {i}")

    _login(monkeypatch, "bob@example.com")
    all_notes = await app_module.kb_notifications(limit=3)
    assert len(all_notes["unread"]) == 3, "limit must cap the rows returned"
    assert all_notes["counts"]["notifications"] >= 6, "counts stay true regardless of limit"

    only_requests = await app_module.kb_notifications(limit=50, kind="contact_request")
    assert only_requests["unread"] and all(
        n["kind"] == "contact_request" for n in only_requests["unread"]
    ), "kind filter must exclude the room_turn backlog, not just the first page of it"


@pytest.mark.asyncio
async def test_dm_requires_contact(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    with pytest.raises(KBError, match="contact"):
        await app_module.kb_dm("bob", "we are not connected yet")


@pytest.mark.asyncio
async def test_dm_secret_scan_blocks_keys(mu, monkeypatch):
    # connect first
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_add_contact("bob")
    _login(monkeypatch, "bob@example.com")
    await app_module.kb_accept_contact("alice")
    _login(monkeypatch, "alice@example.com")
    with pytest.raises(KBError, match="secret"):
        await app_module.kb_dm("bob", "my key is AKIAIOSFODNN7EXAMPLE use it")


@pytest.mark.asyncio
async def test_dm_to_unknown_handle_errors(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    with pytest.raises(KBError, match="No Engram user"):
        await app_module.kb_dm("ghost", "hello?")


@pytest.mark.asyncio
async def test_kb_load_surfaces_social_counts(mu, monkeypatch):
    # bob gets a contact request -> one unread notification
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_add_contact("bob")
    _login(monkeypatch, "bob@example.com")
    # give bob a project to load (fresh tenant brains start empty)
    await app_module.kb_write(
        path="projects/demo/context.md",
        content="---\ntype: project\ndescription: demo\n---\n\n# About\n\nDemo.\n",
        message="feat: demo",
    )
    loaded = await app_module.kb_load("demo", lite=True)
    assert loaded["social"]["unread_notifications"] >= 1


@pytest.mark.asyncio
async def test_social_tools_refuse_without_account(mu, monkeypatch):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="google:stranger@example.com"))
    with pytest.raises(KBError, match="account"):
        await app_module.kb_add_contact("bob")
