"""M0.3 — StoreRegistry: subject -> account -> per-user KBStore (owner unchanged)."""

import asyncio

import pytest

from engram_server.errors import KBError
from engram_server.provisioning import users_root_path
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu_settings(settings, tmp_path):
    """conftest's operator settings, flipped to multiuser with tmp tenant roots."""
    return settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
        }
    )


def _invited_user(registry, handle, email):
    invite = registry.tenancy.create_invite(email)
    return registry.tenancy.accept_invite(
        invite.token, handle, email, "google", f"google:{email}"
    )


def test_owner_store_matches_operator_settings(settings):
    registry = StoreRegistry(settings)
    assert registry.owner.root == settings.brain_path


@pytest.mark.asyncio
async def test_single_user_mode_maps_everyone_to_owner(settings):
    registry = StoreRegistry(settings)
    resolved = await registry.store_for_subject("google:stranger@example.com")
    assert resolved is registry.owner


@pytest.mark.asyncio
async def test_owner_subject_maps_to_owner_in_multiuser(mu_settings):
    registry = StoreRegistry(mu_settings)
    resolved = await registry.store_for_subject("github:metalfinger")
    assert resolved is registry.owner


@pytest.mark.asyncio
async def test_unknown_subject_is_taught_the_invite_flow(mu_settings):
    registry = StoreRegistry(mu_settings)
    with pytest.raises(KBError, match="invite"):
        await registry.store_for_subject("google:stranger@example.com")


@pytest.mark.asyncio
async def test_tenant_store_is_provisioned_lazily_and_cached(mu_settings):
    registry = StoreRegistry(mu_settings)
    _invited_user(registry, "amiyanshu", "a@example.com")

    store = await registry.store_for_subject("google:a@example.com")
    assert store is not registry.owner
    assert store.root == users_root_path(mu_settings) / "amiyanshu" / "brain"
    assert (store.root / "index.md").is_file()
    assert registry.cached_handles() == ["amiyanshu"]

    again = await registry.store_for_subject("google:a@example.com")
    assert again is store

    result = await store.kb_write(
        "projects/demo/context.md",
        "---\ntype: project\ndescription: demo\n---\n\n# About\n\nDemo.\n",
        "feat: demo",
    )
    assert result["pushed"] is True


@pytest.mark.asyncio
async def test_two_tenants_get_disjoint_stores(mu_settings):
    registry = StoreRegistry(mu_settings)
    _invited_user(registry, "alpha", "alpha@example.com")
    _invited_user(registry, "beta", "beta@example.com")

    a = await registry.store_for_subject("google:alpha@example.com")
    b = await registry.store_for_subject("google:beta@example.com")
    assert a is not b
    assert a.root != b.root


@pytest.mark.asyncio
async def test_suspended_account_is_refused(mu_settings):
    registry = StoreRegistry(mu_settings)
    _invited_user(registry, "gamma", "gamma@example.com")
    registry.tenancy.set_status("gamma", "suspended")
    with pytest.raises(KBError, match="suspended"):
        await registry.store_for_subject("google:gamma@example.com")


@pytest.mark.asyncio
async def test_concurrent_first_resolve_provisions_once(mu_settings):
    registry = StoreRegistry(mu_settings)
    _invited_user(registry, "delta", "delta@example.com")

    first, second = await asyncio.gather(
        registry.store_for_subject("google:delta@example.com"),
        registry.store_for_subject("google:delta@example.com"),
    )
    assert first is second
    assert registry.cached_handles() == ["delta"]
