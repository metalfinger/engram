"""#14 — public /share/<token> resolves to the OWNING tenant's brain in multi-user."""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.capabilities import CapabilityStore
from engram_server.registry import StoreRegistry


def test_capability_store_public_share_index(tmp_path):
    cap = CapabilityStore(tmp_path / "engram.db")
    cap.register_public_share("tok-abc", "alice")
    assert cap.resolve_public_share("tok-abc") == "alice"
    assert cap.resolve_public_share("nope") is None
    cap.drop_public_share("tok-abc")
    assert cap.resolve_public_share("tok-abc") is None


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


@pytest.mark.asyncio
async def test_share_registers_token_and_resolver_finds_owner_brain(mu, monkeypatch):
    _login(monkeypatch, "alice@example.com")
    # alice writes an artifact and shares it publicly
    await app_module.kb_write(
        path="projects/demo/artifacts/2026-07-note.md",
        content="---\ntype: artifact\ndescription: a shared note\n---\n\n# Note\n\nHello world, public.\n",
        message="feat: artifact",
    )
    res = await app_module.kb_share_artifact("projects/demo/artifacts/2026-07-note.md")
    token = res["share_url"].rsplit("/", 1)[-1]

    # the token is indexed to alice, and the resolver points at HER brain
    assert mu.capabilities.resolve_public_share(token) == "alice"
    alice_root = (await mu.store_for_handle("alice")).root
    assert await app_module._share_resolver(token) == alice_root

    # an unknown token falls back to the owner brain (legacy single-user shares)
    assert await app_module._share_resolver("x" * 30) == mu.owner.root


@pytest.mark.asyncio
async def test_two_tenants_tokens_resolve_to_their_own_brains(mu, monkeypatch):
    mu.capabilities.register_public_share("tok-alice", "alice")
    mu.capabilities.register_public_share("tok-bob", "bob")
    a = await app_module._share_resolver("tok-alice")
    b = await app_module._share_resolver("tok-bob")
    assert a != b
    assert a == (await mu.store_for_handle("alice")).root
    assert b == (await mu.store_for_handle("bob")).root
