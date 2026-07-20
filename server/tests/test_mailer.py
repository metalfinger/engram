"""Tests for engram_server.mailer — zero-network invite email sending.

Covers the pure body builder plus the three send_invite paths: disabled
(no api token -> no network call at all), success, and failure (bad status
or a raised exception) -- send_invite must never raise.
"""

from __future__ import annotations

from engram_server.config import Settings
from engram_server.mailer import build_invite_email, send_invite


def _settings(**overrides) -> Settings:
    defaults = dict(
        cf_email_api_token="",
        cf_email_account_id="",
        invite_from_email="no-reply@metalfinger.xyz",
        invite_from_name="Engram",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeClient:
    """Minimal async httpx.AsyncClient stand-in that records the call it received."""

    def __init__(self, status_code: int = 202, raise_exc: Exception | None = None):
        self.status_code = status_code
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raise_exc:
            raise self.raise_exc
        return FakeResponse(self.status_code)


class NeverCalledClient:
    """Proves the disabled path never touches the network."""

    async def post(self, *args, **kwargs):
        raise AssertionError("http.post must not be called when the mailer is disabled")


JOIN_URL = "https://engram.metalfinger.xyz/join?token=abc123"


# --- build_invite_email ---------------------------------------------------


def test_build_invite_email_has_subject_and_join_url_in_both_bodies():
    body = build_invite_email(JOIN_URL, None, "Engram")
    assert body["subject"]
    assert JOIN_URL in body["text"]
    assert JOIN_URL in body["html"]
    assert f'<a href="{JOIN_URL}"' in body["html"]


def test_build_invite_email_includes_inviter_name_when_given():
    body = build_invite_email(JOIN_URL, "Hiren", "Engram")
    assert "Hiren" in body["text"]
    assert "Hiren" in body["html"]


def test_build_invite_email_omits_inviter_gracefully_when_none():
    body = build_invite_email(JOIN_URL, None, "Engram")
    assert "None" not in body["text"]
    assert "None" not in body["html"]


# --- send_invite: disabled (default state) --------------------------------


async def test_send_invite_disabled_returns_link_without_network_call():
    settings = _settings(cf_email_api_token="")
    result = await send_invite(
        settings,
        to_email="new@example.com",
        join_url=JOIN_URL,
        http=NeverCalledClient(),
    )
    assert result["sent"] is False
    assert "manual" in result["reason"]
    assert "link" in result["reason"]
    assert result["join_url"] == JOIN_URL


# --- send_invite: success --------------------------------------------------


async def test_send_invite_success_posts_expected_auth_and_payload():
    settings = _settings(cf_email_api_token="tok123", cf_email_account_id="acct1")
    fake = FakeClient(status_code=202)

    result = await send_invite(
        settings,
        to_email="new@example.com",
        join_url=JOIN_URL,
        inviter_name="Hiren",
        http=fake,
    )

    assert result == {"sent": True, "to": "new@example.com"}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tok123"
    assert call["json"]["to"] == "new@example.com"
    assert call["json"]["from"] == {"address": "no-reply@metalfinger.xyz", "name": "Engram"}
    assert JOIN_URL in call["json"]["html"]
    # token must never leak back out through the return value
    assert "tok123" not in str(result)


# --- send_invite: failure ---------------------------------------------------


async def test_send_invite_bad_status_returns_sent_false_without_raising():
    settings = _settings(cf_email_api_token="tok123", cf_email_account_id="acct1")
    fake = FakeClient(status_code=500)

    result = await send_invite(
        settings, to_email="new@example.com", join_url=JOIN_URL, http=fake
    )

    assert result["sent"] is False
    assert result["reason"]
    assert result["join_url"] == JOIN_URL


async def test_send_invite_exception_returns_sent_false_without_raising():
    settings = _settings(cf_email_api_token="tok123", cf_email_account_id="acct1")
    fake = FakeClient(raise_exc=RuntimeError("boom"))

    result = await send_invite(
        settings, to_email="new@example.com", join_url=JOIN_URL, http=fake
    )

    assert result["sent"] is False
    assert "boom" in result["reason"]
    assert result["join_url"] == JOIN_URL
