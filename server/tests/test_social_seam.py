"""M2.3 — current_user() identity seam + owner-account bootstrap."""

from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu_registry(settings, tmp_path, monkeypatch):
    mu = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
    })
    registry = StoreRegistry(mu)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", mu)
    return registry


def _login(monkeypatch, subject):
    token = None if subject is None else SimpleNamespace(subject=subject)
    monkeypatch.setattr(app_module, "get_access_token", lambda: token)


def test_ensure_owner_account_is_idempotent(mu_registry):
    a = mu_registry.ensure_owner_account()
    b = mu_registry.ensure_owner_account()
    assert a.id == b.id
    assert a.handle == "hiren"
    assert mu_registry.tenancy.user_by_subject("github:metalfinger").id == a.id


def test_current_user_resolves_owner_after_bootstrap(mu_registry, monkeypatch):
    mu_registry.ensure_owner_account()
    _login(monkeypatch, "github:metalfinger")
    user = app_module.current_user()
    assert user is not None and user.handle == "hiren"


def test_current_user_none_without_token(mu_registry, monkeypatch):
    _login(monkeypatch, None)
    assert app_module.current_user() is None


def test_current_user_none_for_unknown_subject(mu_registry, monkeypatch):
    _login(monkeypatch, "google:stranger@example.com")
    assert app_module.current_user() is None


def test_current_user_resolves_tenant(mu_registry, monkeypatch):
    inv = mu_registry.tenancy.create_invite("a@example.com")
    mu_registry.tenancy.accept_invite(inv.token, "amiya", "a@example.com", "google", "google:a@example.com")
    _login(monkeypatch, "google:a@example.com")
    user = app_module.current_user()
    assert user.handle == "amiya"
