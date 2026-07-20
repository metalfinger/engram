"""M1.4 — Dashboard + onboarding: session cookies, invite claim, access control."""

from types import SimpleNamespace

import pytest

from engram_server.dashboard import Dashboard, DashboardAuth
from engram_server.provisioning import users_root_path
from engram_server.registry import StoreRegistry
from engram_server.tenancy import TenancyError


class FakeIdP:
    def __init__(self, name, login):
        self.name = name
        self._login = login

    def authorize_url(self, redirect_uri, state):
        return f"https://{self.name}.example/auth?state={state}"

    async def exchange_code(self, code, redirect_uri, http):
        return "upstream-token"

    async def fetch_user(self, token, http):
        return SimpleNamespace(id="x", login=self._login)


async def _fake_mailer(settings, *, to_email, join_url, inviter_name=None):
    return {"sent": True, "to": to_email}


@pytest.fixture()
def mu_settings(settings, tmp_path):
    return settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
        "dashboard_session_secret": "test-secret-please-change",
    })


@pytest.fixture()
def dash(mu_settings):
    registry = StoreRegistry(mu_settings)
    idps = {"google": FakeIdP("google", "a@example.com")}
    return Dashboard(mu_settings, registry, idps, mailer=_fake_mailer)


# -- session cookie ---------------------------------------------------------


def test_session_roundtrip_and_tamper(mu_settings):
    auth = DashboardAuth("secret-A", 3600)
    cookie = auth.issue("google:a@example.com", "a@example.com", "amiya")
    assert auth.verify(cookie)["sub"] == "google:a@example.com"
    # wrong secret rejects
    assert DashboardAuth("secret-B", 3600).verify(cookie) is None
    # garbage rejects; empty rejects
    assert auth.verify("not-a-jwt") is None
    assert auth.verify(None) is None


def test_session_expiry(mu_settings):
    auth = DashboardAuth("secret", -1)  # already expired on issue
    assert auth.verify(auth.issue("google:a@example.com", "a@example.com", "amiya")) is None


# -- invite claim -----------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_creates_account_and_brain(dash):
    invite = dash.registry.tenancy.create_invite("a@example.com")
    handle = await dash._claim(invite.token, "amiya", "google", "google:a@example.com")
    assert handle == "amiya"
    # account exists, brain provisioned
    user = dash.registry.tenancy.user_by_subject("google:a@example.com")
    assert user.handle == "amiya" and user.email == "a@example.com"
    assert (users_root_path(dash.settings) / "amiya" / "brain" / "index.md").is_file()
    # invite consumed
    assert not dash.registry.tenancy.get_invite(invite.token).live


@pytest.mark.asyncio
async def test_claim_rejects_dead_invite(dash):
    invite = dash.registry.tenancy.create_invite("a@example.com")
    dash.registry.tenancy.revoke_invite(invite.token)
    with pytest.raises(TenancyError):
        await dash._claim(invite.token, "amiya", "google", "google:a@example.com")


@pytest.mark.asyncio
async def test_claim_rejects_reserved_and_device_handles(dash):
    for bad in ("hiren", "con", "engram"):
        invite = dash.registry.tenancy.create_invite(f"{bad}@example.com")
        with pytest.raises(TenancyError):
            await dash._claim(invite.token, bad, "google", f"google:{bad}@example.com")


# -- invite creation (owner) ------------------------------------------------


@pytest.mark.asyncio
async def test_create_invite_emails_and_records_inviter(dash):
    owner = {"sub": "github:metalfinger", "handle": "hiren"}
    result = await dash._create_invite("new@example.com", owner)
    assert result["mail"]["sent"] is True
    assert result["join_url"].endswith(result["invite"].token)
    assert "/join?token=" in result["join_url"]


# -- access control ---------------------------------------------------------


def test_is_owner(dash):
    assert dash._is_owner({"sub": "github:metalfinger"})
    assert not dash._is_owner({"sub": "google:a@example.com"})


def test_account_handle_owner_and_tenant(dash):
    assert dash._account_handle({"sub": "github:metalfinger"}) == "hiren"
    assert dash._account_handle({"sub": "google:nobody@example.com"}) is None
    inv = dash.registry.tenancy.create_invite("a@example.com")
    dash.registry.tenancy.accept_invite(inv.token, "amiya", "a@example.com", "google", "google:a@example.com")
    assert dash._account_handle({"sub": "google:a@example.com"}) == "amiya"


@pytest.mark.asyncio
async def test_create_invite_denied_for_non_owner(dash):
    """A member session cannot create invites via the route handler."""
    class Req:
        cookies = {"engram_session": dash.auth.issue("google:a@example.com", "a@example.com", "amiya")}
        async def form(self):
            return {"email": "x@example.com"}
    resp = await dash.create_invite(Req())
    assert resp.status_code == 403


def test_dashboard_renders_admin_only_for_owner(dash):
    owner_html = dash._render_dashboard({"sub": "github:metalfinger"})
    assert "Invite someone" in owner_html and "Members" in owner_html
    # a member sees the connect block but not the admin panel
    inv = dash.registry.tenancy.create_invite("a@example.com")
    dash.registry.tenancy.accept_invite(inv.token, "amiya", "a@example.com", "google", "google:a@example.com")
    member_html = dash._render_dashboard({"sub": "google:a@example.com"})
    assert "Connect your AI" in member_html and "Invite someone" not in member_html


def test_register_dashboard_noop_single_user(settings):
    from engram_server.dashboard import register_dashboard
    registry = StoreRegistry(settings)  # multiuser=False
    assert register_dashboard(None, settings, registry, {"google": FakeIdP("google", "x")}) is None
