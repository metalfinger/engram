"""Harness lifecycle over HTTP — the second door, and why it is safe.

Pi's ExtensionAPI has no tool-execution method, so a boot hook that must call
kb_realign deterministically before the model's first turn cannot go through the
MCP adapter at all. The alternative was ~150 lines of hand-rolled protocol living
inside the harness — a second implementation of the wire format, in the
safety-relevant layer, drifting the moment anything moved.

The rule these tests pin: model-facing -> MCP (definitions live on the server and
must not rot in copies); code-facing lifecycle calls -> HTTP (no model, no
descriptions, the handshake buys nothing). One implementation, two doors.
"""

import subprocess
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import engram_server.app as app_module
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    s = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
        "dashboard_session_secret": "z" * 40,
    })
    registry = StoreRegistry(s)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", s)
    monkeypatch.setattr(app_module, "_presence_last", {})
    for h, e in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
        inv = registry.tenancy.create_invite(e)
        registry.tenancy.accept_invite(inv.token, h, e, "github", f"github:{h}")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="github:alice"))
    return registry


def _token(scope="harness", subject="github:alice"):
    return app_module._dashboard.auth.issue(
        subject, "alice@example.com", "alice", ttl=3600, scope=scope,
    )


@pytest.fixture()
def client():
    return TestClient(app_module.mcp.streamable_http_app())


def test_realign_needs_a_harness_token(mu, client):
    r = client.post("/api/session/realign", json={"project": "alt"})
    assert r.status_code == 401


def test_a_notify_token_cannot_reach_the_harness_routes(mu, client):
    """THE POINT OF A SEPARATE SCOPE. The notify token sits in a config file on
    every machine and was minted for 'read notifications, post presence'. If it
    reached kb_finish_session — which writes a log, closes a thread and releases
    claims — every machine token on every PC would retroactively gain powers
    nobody approved when they were minted."""
    r = client.post(
        "/api/session/finish",
        json={"thread": "t", "project": "alt"},
        headers={"Authorization": f"Bearer {_token(scope='notify')}"},
    )
    assert r.status_code == 401


def test_a_session_cookie_token_cannot_be_replayed_here(mu, client):
    r = client.post(
        "/api/session/realign", json={"project": "alt"},
        headers={"Authorization": f"Bearer {_token(scope='session')}"},
    )
    assert r.status_code == 401


def test_realign_returns_what_the_tool_returns(mu, client):
    r = client.post(
        "/api/session/realign", json={"project": "alt", "cwd": "/tmp/x"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    body = r.json()
    # Same shape as the tool — these call the same function, so a divergence here
    # means somebody reimplemented instead of delegating.
    assert "resolved" in body


def test_a_bad_body_is_a_message_not_a_stack(mu, client):
    """The caller is a hook deciding whether to continue. A failure it cannot
    parse gets treated as success, which is the worst outcome available."""
    r = client.post(
        "/api/session/realign", data="not json",
        headers={"Authorization": f"Bearer {_token()}",
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "error" in r.json()


def test_the_impersonation_context_does_not_leak(mu, client):
    """These routes share a worker with everything else, so a context left set
    would hand the NEXT request somebody else's identity."""
    from mcp.server.auth.middleware.auth_context import auth_context_var

    before = auth_context_var.get()
    r = client.post(
        "/api/session/realign", json={"project": "alt"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code == 200
    assert auth_context_var.get() is before


@pytest.mark.asyncio
async def test_setup_hands_out_a_separate_harness_token(mu):
    out = await app_module.kb_setup_machine()
    h = out["harness"]
    assert h["scope"] == "harness"
    assert h["token"] and h["token"] != out["token"], "must not be the same token"
    assert h["routes"]["realign"].endswith("/api/session/realign")
    # Each token opens exactly its own door.
    auth = app_module._dashboard.auth
    assert auth.verify(h["token"], expected_scope="harness") is not None
    assert auth.verify(h["token"], expected_scope="notify") is None
    assert auth.verify(out["token"], expected_scope="harness") is None


@pytest.mark.asyncio
async def test_impersonation_really_selects_the_caller(mu, monkeypatch):
    """The test above returns 200 whether or not impersonation works, because the
    fixture monkeypatches get_access_token — so it proves the route runs, not that
    it runs AS THE RIGHT PERSON. This one restores the real SDK lookup and asserts
    the seam itself: without it, every harness call would silently execute as
    whoever the context happened to hold."""
    from mcp.server.auth.middleware.auth_context import (
        auth_context_var,
        get_access_token as real_get_access_token,
    )

    monkeypatch.setattr(app_module, "get_access_token", real_get_access_token)
    assert auth_context_var.get() is None, "clean slate"

    async with app_module._acting_as("github:bob"):
        assert app_module._require_user().handle == "bob"
    async with app_module._acting_as("github:alice"):
        assert app_module._require_user().handle == "alice"

    # And it is genuinely put back, not merely overwritten by the next caller.
    assert auth_context_var.get() is None


@pytest.mark.asyncio
async def test_a_token_for_a_deleted_account_opens_nothing(mu):
    """A signed token outlives the account it names. Verifying the signature is
    not the same as verifying the caller still exists."""

    class _Req:
        headers = {"authorization": f"Bearer {_token(subject='github:ghost')}"}

    assert app_module._harness_subject(_Req()) == ""
