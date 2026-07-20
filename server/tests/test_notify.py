"""Tests for engram_server.notify — zero-network notification push fanout.

Covers the pure email body builder plus fanout's channel matrix: all-disabled
(no network calls at all), email-only, telegram-only, prefs opt-out, and
failure isolation (one channel's failure never blocks another, and fanout
never raises).
"""

from __future__ import annotations

from engram_server.config import Settings
from engram_server.notify import Recipient, build_notification_email, fanout


def _settings(**overrides) -> Settings:
    defaults = dict(
        cf_email_api_token="",
        cf_email_account_id="",
        invite_from_email="no-reply@metalfinger.xyz",
        invite_from_name="Engram",
        telegram_bot_token="",
        public_url="https://engram.metalfinger.xyz",
    )
    defaults.update(overrides)
    return Settings(**defaults)


NOTIFICATION = {"kind": "dm", "body": "Hiren sent you a message", "ref": "msg-123"}


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeClient:
    """Minimal async httpx.AsyncClient stand-in that records the calls it received."""

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
    """Proves the all-disabled path never touches the network."""

    async def post(self, *args, **kwargs):
        raise AssertionError("http.post must not be called when a channel is disabled")


class PerUrlClient:
    """Routes to different fake behavior depending on which API is hit, so a
    single fanout call can exercise "email fails, telegram succeeds" in one
    shot (failure isolation)."""

    def __init__(self, email_status=500, telegram_status=200, email_raises=None):
        self.calls: list[dict] = []
        self.email_status = email_status
        self.telegram_status = telegram_status
        self.email_raises = email_raises

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if "api.telegram.org" in url:
            return FakeResponse(self.telegram_status)
        if self.email_raises:
            raise self.email_raises
        return FakeResponse(self.email_status)


def _recipient(**overrides) -> Recipient:
    defaults = dict(
        handle="hiren",
        email="hir@example.com",
        telegram_chat_id="12345",
        prefs={},
    )
    defaults.update(overrides)
    return Recipient(**defaults)


# --- build_notification_email ----------------------------------------------


def test_build_notification_email_has_subject_body_and_dashboard_link():
    dashboard_url = "https://engram.metalfinger.xyz/dashboard"
    body = build_notification_email(NOTIFICATION, "Engram", dashboard_url)
    assert body["subject"]
    assert NOTIFICATION["body"] in body["text"]
    assert NOTIFICATION["body"] in body["html"]
    assert dashboard_url in body["text"]
    assert dashboard_url in body["html"]


def test_build_notification_email_escapes_html_injection():
    """A DM body is attacker-controlled — it must not inject markup into the email."""
    evil = {"kind": "dm", "body": "<script>alert(1)</script><img src=x onerror=y>", "ref": None}
    out = build_notification_email(evil, "Engram", "https://engram.metalfinger.xyz/dashboard")
    assert "<script>" not in out["html"]
    assert "&lt;script&gt;" in out["html"]
    assert "onerror=y>" not in out["html"]
    # plain-text part carries the raw body (no HTML context there) — that's fine
    assert "<script>" in out["text"]


# --- fanout: all disabled ----------------------------------------------------


async def test_fanout_all_disabled_makes_zero_network_calls():
    settings = _settings(cf_email_api_token="", telegram_bot_token="")
    recipient = _recipient()

    result = await fanout(settings, recipient, NOTIFICATION, http=NeverCalledClient())

    channels = result["channels"]
    assert channels["dashboard"]["sent"] is True
    assert channels["email"]["sent"] is False
    assert channels["email"]["reason"]
    assert channels["telegram"]["sent"] is False
    assert channels["telegram"]["reason"]


# --- fanout: email only configured ------------------------------------------


async def test_fanout_email_only_posts_expected_auth_and_payload():
    settings = _settings(
        cf_email_api_token="tok123", cf_email_account_id="acct1", telegram_bot_token=""
    )
    recipient = _recipient()
    fake = FakeClient(status_code=202)

    result = await fanout(settings, recipient, NOTIFICATION, http=fake)

    assert result["channels"]["email"]["sent"] is True
    assert result["channels"]["telegram"]["sent"] is False
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tok123"
    assert call["json"]["to"] == "hir@example.com"
    assert call["json"]["from"] == {"address": "no-reply@metalfinger.xyz", "name": "Engram"}
    # token must never leak back out through the return value
    assert "tok123" not in str(result)


# --- fanout: telegram only configured ---------------------------------------


async def test_fanout_telegram_only_posts_expected_chat_id_and_text():
    settings = _settings(cf_email_api_token="", telegram_bot_token="bot-tok-999")
    recipient = _recipient()
    fake = FakeClient(status_code=200)

    result = await fanout(settings, recipient, NOTIFICATION, http=fake)

    assert result["channels"]["telegram"]["sent"] is True
    assert result["channels"]["email"]["sent"] is False
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://api.telegram.org/botbot-tok-999/sendMessage"
    assert call["json"]["chat_id"] == "12345"
    assert NOTIFICATION["body"] in call["json"]["text"]
    # token must never leak back out through the return value
    assert "bot-tok-999" not in str(result)


# --- fanout: prefs opt-out ----------------------------------------------------


async def test_fanout_email_opt_out_skips_send_without_network_call():
    settings = _settings(cf_email_api_token="tok123", cf_email_account_id="acct1")
    recipient = _recipient(prefs={"email": False})

    result = await fanout(settings, recipient, NOTIFICATION, http=NeverCalledClient())

    assert result["channels"]["email"]["sent"] is False
    assert "opt" in result["channels"]["email"]["reason"].lower()


# --- fanout: failure isolation ------------------------------------------------


async def test_fanout_email_failure_does_not_block_telegram_or_dashboard():
    settings = _settings(
        cf_email_api_token="tok123",
        cf_email_account_id="acct1",
        telegram_bot_token="bot-tok-999",
    )
    recipient = _recipient()
    fake = PerUrlClient(email_status=500, telegram_status=200)

    result = await fanout(settings, recipient, NOTIFICATION, http=fake)

    channels = result["channels"]
    assert channels["email"]["sent"] is False
    assert channels["telegram"]["sent"] is True
    assert channels["dashboard"]["sent"] is True


async def test_fanout_email_exception_does_not_raise_or_block_telegram():
    settings = _settings(
        cf_email_api_token="tok123",
        cf_email_account_id="acct1",
        telegram_bot_token="bot-tok-999",
    )
    recipient = _recipient()
    fake = PerUrlClient(email_raises=RuntimeError("boom"), telegram_status=200)

    result = await fanout(settings, recipient, NOTIFICATION, http=fake)

    channels = result["channels"]
    assert channels["email"]["sent"] is False
    assert "boom" in channels["email"]["reason"]
    assert channels["telegram"]["sent"] is True
    assert channels["dashboard"]["sent"] is True
