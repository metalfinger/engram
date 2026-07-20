"""M3.2 — context-sharing tools: grant/request/guest-read/guest-search/kb_send + adversarial scope."""

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
    for handle, email in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(email)
        registry.tenancy.accept_invite(inv.token, handle, email, "google", f"google:{email}")
    return registry


def _login(monkeypatch, email):
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject=f"google:{email}"))


async def _alice_writes(monkeypatch, path, desc, body="Secret content."):
    _login(monkeypatch, "alice@example.com")
    await app_module.kb_write(
        path=path,
        content=f"---\ntype: project\ndescription: {desc}\n---\n\n# About\n\n{body}\n",
        message="feat: seed",
    )


@pytest.mark.asyncio
async def test_share_then_guest_read(mu, monkeypatch):
    await _alice_writes(monkeypatch, "projects/alt/context.md", "alt", "the alt plan")
    # alice shares projects/alt with bob
    r = await app_module.kb_share_context("bob", ["projects/alt"])
    assert r["granted_to"] == "bob"
    # bob reads it directly
    _login(monkeypatch, "bob@example.com")
    got = await app_module.kb_guest_read("alice", "projects/alt/context.md")
    assert "the alt plan" in got["content"]
    # and it shows in shared-with-me
    assert any(g["from"] == "alice" for g in (await app_module.kb_shared_with_me())["grants"])


@pytest.mark.asyncio
async def test_guest_read_refused_outside_grant(mu, monkeypatch):
    await _alice_writes(monkeypatch, "projects/alt/context.md", "alt")
    await _alice_writes(monkeypatch, "projects/secret/context.md", "secret", "PRIVATE")
    await app_module.kb_share_context("bob", ["projects/alt"])
    _login(monkeypatch, "bob@example.com")
    # the sibling project is NOT covered
    with pytest.raises(KBError, match="grant covering"):
        await app_module.kb_guest_read("alice", "projects/secret/context.md")


@pytest.mark.asyncio
async def test_guest_read_prefix_boundary(mu, monkeypatch):
    # a grant on "projects/alt" must NOT cover "projects/alt-secret"
    await _alice_writes(monkeypatch, "projects/alt-secret/context.md", "altsecret", "NOPE")
    await _alice_writes(monkeypatch, "projects/alt/context.md", "alt")
    await app_module.kb_share_context("bob", ["projects/alt"])
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_guest_read("alice", "projects/alt-secret/context.md")


@pytest.mark.asyncio
async def test_guest_read_traversal_refused(mu, monkeypatch):
    await _alice_writes(monkeypatch, "projects/alt/context.md", "alt")
    await app_module.kb_share_context("bob", ["projects/alt"])
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_guest_read("alice", "../../etc/passwd")


@pytest.mark.asyncio
async def test_guest_read_refused_after_revoke(mu, monkeypatch):
    await _alice_writes(monkeypatch, "projects/alt/context.md", "alt")
    await app_module.kb_share_context("bob", ["projects/alt"])
    # alice revokes
    _login(monkeypatch, "alice@example.com")
    me = app_module.current_user()
    bob = mu.tenancy.user_by_handle("bob")
    cap = mu.capabilities.list_granted_by(me.id)[0]
    mu.capabilities.revoke(cap.id, me.id)
    _login(monkeypatch, "bob@example.com")
    with pytest.raises(KBError):
        await app_module.kb_guest_read("alice", "projects/alt/context.md")


@pytest.mark.asyncio
async def test_request_then_grant_flow(mu, monkeypatch):
    await _alice_writes(monkeypatch, "projects/alt/context.md", "alt", "plan")
    # bob requests access
    _login(monkeypatch, "bob@example.com")
    req = await app_module.kb_request_context("alice", ["projects/alt"], reason="reviewing")
    assert req["status"] == "pending"
    # alice sees it + approves
    _login(monkeypatch, "alice@example.com")
    granted = await app_module.kb_grant_request("bob", approve=True)
    assert granted["status"] == "approved"
    # bob can now read
    _login(monkeypatch, "bob@example.com")
    got = await app_module.kb_guest_read("alice", "projects/alt/context.md")
    assert "plan" in got["content"]


@pytest.mark.asyncio
async def test_kb_send_requires_contact_then_lands_in_inbox(mu, monkeypatch):
    await _alice_writes(monkeypatch, "projects/alt/note.md", "note", "handoff details")
    # not contacts yet
    _login(monkeypatch, "alice@example.com")
    with pytest.raises(KBError, match="contact"):
        await app_module.kb_send("bob", "projects/alt/note.md")
    # connect
    await app_module.kb_add_contact("bob")
    _login(monkeypatch, "bob@example.com")
    await app_module.kb_accept_contact("alice")
    # now alice sends
    _login(monkeypatch, "alice@example.com")
    sent = await app_module.kb_send("bob", "projects/alt/note.md")
    assert sent["sent_to"] == "bob"
    # bob's brain now has it in inbox with provenance
    _login(monkeypatch, "bob@example.com")
    got = await (await app_module.current_store()).kb_read(sent["as_path"])
    assert "adopted_from: brain://alice/projects/alt/note.md" in got["content"]
    assert "handoff details" in got["content"]
