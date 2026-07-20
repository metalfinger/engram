"""Invite email mailer (Cloudflare Email Sending REST API).

Sends the "you're invited to Engram" email when an operator creates an invite
(tenancy.create_invite -> token) so the invitee can click a magic link
straight to /join?token=... . Degrades gracefully when unconfigured: an empty
``cf_email_api_token`` is the expected default state, not an error — the
dashboard just shows the join link for the operator to copy/paste instead of
emailing it. A failed send must never break invite creation, so this module
never raises; every path returns a small ``{sent, ...}`` dict.

CF_EMAIL_ENDPOINT below is the Cloudflare Email Sending REST API's send
endpoint, confirmed against Cloudflare's current docs (not a guess): `from`
uses `address`/`name` (not Workers-binding `email`), auth is a bearer API
token with email-sending permission, and success/failure both come back as
JSON with an HTTP status. If Cloudflare changes this shape, this constant is
the one place to fix it.
"""

from __future__ import annotations

import html as _html
import logging
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("engram.mailer")

CF_EMAIL_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send"
)


def build_invite_email(
    join_url: str, inviter_name: str | None, from_name: str
) -> dict[str, str]:
    """Pure body builder — no network, no settings — so subject/text/html can
    be unit-tested directly. ``inviter_name`` is optional: some invites are
    operator-issued with no named inviter, and the copy reads fine either way."""
    subject = "You're invited to Engram"
    lead = f"{inviter_name} invited you to join" if inviter_name else "You've been invited to join"

    text = (
        f"{lead} {from_name}, a private cross-session knowledge base.\n\n"
        f"Accept your invite:\n{join_url}\n\n"
        "If you weren't expecting this, you can ignore this email."
    )

    # Escape every interpolated value before embedding in HTML (defense-in-depth:
    # inviter_name is a validated handle and join_url is server-built, but the HTML
    # email should never trust an interpolated string).
    safe_lead = _html.escape(lead)
    safe_from = _html.escape(from_name)
    safe_url = _html.escape(join_url, quote=True)
    html = (
        '<div style="font-family: -apple-system, Segoe UI, sans-serif; '
        'max-width: 480px; margin: 0 auto; padding: 24px; color: #1a1a1a;">'
        f"<p style=\"font-size: 15px;\">{safe_lead} <strong>{safe_from}</strong>, "
        "a private cross-session knowledge base.</p>"
        '<p style="margin: 24px 0;">'
        f'<a href="{safe_url}" style="background: #2563eb; color: #ffffff; '
        'padding: 10px 20px; border-radius: 6px; text-decoration: none; '
        f'font-size: 14px; display: inline-block;">Accept invite</a></p>'
        '<p style="font-size: 13px; color: #666;">Or copy this link: '
        f'<a href="{safe_url}">{safe_url}</a></p>'
        "</div>"
    )

    return {"subject": subject, "text": text, "html": html}


async def send_invite(
    settings: Settings,
    *,
    to_email: str,
    join_url: str,
    inviter_name: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Send the invite email, or degrade gracefully when the mailer isn't
    configured. Never raises — a failed send must not break invite creation;
    the operator can always fall back to copying ``join_url``.

    ``http`` is injectable (an httpx.AsyncClient or test fake exposing an
    async ``post(url, json=..., headers=...)``); tests pass a fake to
    intercept the call. When ``None``, a real client is opened for the call.
    """
    if not settings.cf_email_api_token:
        return {
            "sent": False,
            "reason": "mailer disabled (no ENGRAM_CF_EMAIL_API_TOKEN); share the join link manually",
            "join_url": join_url,
        }

    body = build_invite_email(join_url, inviter_name, settings.invite_from_name)
    payload = {
        "to": to_email,
        "from": {
            "address": settings.invite_from_email,
            "name": settings.invite_from_name,
        },
        "subject": body["subject"],
        "text": body["text"],
        "html": body["html"],
    }
    url = CF_EMAIL_ENDPOINT.format(account_id=settings.cf_email_account_id)
    # Never log the token — it's only ever placed in this one request header.
    headers = {
        "Authorization": f"Bearer {settings.cf_email_api_token}",
        "Content-Type": "application/json",
    }

    async def _send(client: Any) -> dict[str, Any]:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # network error, timeout, etc.
            log.warning("invite email send failed for %s: %s", to_email, exc)
            return {"sent": False, "reason": f"send failed: {exc}", "join_url": join_url}
        if resp.status_code // 100 != 2:
            log.warning(
                "invite email send to %s got status %s", to_email, resp.status_code
            )
            return {
                "sent": False,
                "reason": f"Cloudflare returned status {resp.status_code}",
                "join_url": join_url,
            }
        return {"sent": True, "to": to_email}

    if http is not None:
        return await _send(http)
    async with httpx.AsyncClient() as client:
        return await _send(client)
