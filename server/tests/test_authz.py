"""M0.4 — current_store(): every tool call resolves the CALLER's store.

These tests patch the app module's registry + auth-context reader with
test-scoped doubles; the module-level owner store is never touched (its brain
path points at the operator's real checkout).
"""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.errors import KBError
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu_registry(settings, tmp_path, monkeypatch):
    """A multiuser registry over tmp roots, patched into the app module."""
    mu = settings.model_copy(
        update={
            "multiuser": True,
            "users_root": str(tmp_path / "users"),
            "tenancy_db_path": str(tmp_path / "engram.db"),
        }
    )
    registry = StoreRegistry(mu)
    monkeypatch.setattr(app_module, "registry", registry)
    return registry


def _login(monkeypatch, subject):
    token = None if subject is None else SimpleNamespace(subject=subject)
    monkeypatch.setattr(app_module, "get_access_token", lambda: token)


def _invited_user(registry, handle, email):
    invite = registry.tenancy.create_invite(email)
    return registry.tenancy.accept_invite(
        invite.token, handle, email, "google", f"google:{email}"
    )


@pytest.mark.asyncio
async def test_no_auth_context_resolves_to_owner(mu_registry, monkeypatch):
    _login(monkeypatch, None)
    assert await app_module.current_store() is mu_registry.owner


@pytest.mark.asyncio
async def test_empty_subject_resolves_to_owner(mu_registry, monkeypatch):
    _login(monkeypatch, "")
    assert await app_module.current_store() is mu_registry.owner


@pytest.mark.asyncio
async def test_owner_subject_resolves_to_owner(mu_registry, monkeypatch):
    _login(monkeypatch, "github:metalfinger")
    assert await app_module.current_store() is mu_registry.owner


@pytest.mark.asyncio
async def test_unknown_subject_is_refused(mu_registry, monkeypatch):
    _login(monkeypatch, "google:stranger@example.com")
    with pytest.raises(KBError, match="invite"):
        await app_module.current_store()


@pytest.mark.asyncio
async def test_tenant_subject_resolves_to_their_own_store(mu_registry, monkeypatch):
    _invited_user(mu_registry, "amiyanshu", "a@example.com")
    _login(monkeypatch, "google:a@example.com")

    s = await app_module.current_store()
    assert s is not mu_registry.owner
    assert s.root.parts[-2:] == ("amiyanshu", "brain")


@pytest.mark.asyncio
async def test_tool_call_lands_in_the_callers_brain(mu_registry, monkeypatch):
    """End-to-end through a real registered tool body: the tenant's kb_write goes
    to the tenant's checkout, and their kb_projects sees only their projects."""
    _invited_user(mu_registry, "amiyanshu", "a@example.com")
    _login(monkeypatch, "google:a@example.com")

    await app_module.kb_write(
        path="projects/secret/context.md",
        content="---\ntype: project\ndescription: tenant-private\n---\n\n# About\n\nMine.\n",
        message="feat: tenant project",
    )
    tenant_store = await app_module.current_store()
    assert (tenant_store.root / "projects" / "secret" / "context.md").is_file()
    # ...and nothing was written into the operator's brain path
    assert not (mu_registry.owner.root / "projects" / "secret").exists()

    ids = [p["id"] for p in await app_module.kb_projects()]
    assert "secret" in ids


@pytest.mark.asyncio
async def test_two_subjects_switch_stores_per_call(mu_registry, monkeypatch):
    _invited_user(mu_registry, "alpha", "alpha@example.com")
    _invited_user(mu_registry, "beta", "beta@example.com")

    _login(monkeypatch, "google:alpha@example.com")
    a = await app_module.current_store()
    _login(monkeypatch, "google:beta@example.com")
    b = await app_module.current_store()
    assert a is not b and a.root != b.root
