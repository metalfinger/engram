"""Notification push fanout — delivers an already-persisted notification to a
user's out-of-band channels (dashboard badge, email, Telegram) so it reaches
them with Claude closed.

Persistence is a sibling module's job (social.py writes the notification row);
this module ONLY fans a notification dict back out over channels, honoring
per-user prefs. Mirrors mailer.py's error discipline: every channel failure is
caught and reported as ``{"sent": False, "reason": ...}``, never raised — one
channel's outage must never block another or bubble up to the caller.
"""

from __future__ import annotations

import html as _html
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .mailer import CF_EMAIL_ENDPOINT

log = logging.getLogger("engram.notify")

TELEGRAM_API = "https://api.telegram.org"

_KIND_SUBJECTS = {
    "dm": "New message on Engram",
    "invite_accepted": "Invite accepted on Engram",
    "contact_request": "New contact request on Engram",
}


@dataclass(frozen=True)
class Recipient:
    """Who a notification is being delivered to, and how they want it delivered.

    ``prefs`` is a plain dict like ``{"email": True, "telegram": True}`` — a
    missing key defaults to on, so a recipient with no prefs recorded yet
    still gets notified everywhere they're configured to receive things.
    """

    handle: str
    email: str | None
    telegram_chat_id: str | None
    prefs: dict[str, bool] = field(default_factory=dict)


def build_notification_email(
    notification: dict[str, Any], from_name: str, dashboard_url: str
) -> dict[str, str]:
    """Pure body builder — no network, no settings — so subject/text/html can
    be unit-tested directly, same shape as mailer.build_invite_email."""
    kind = notification.get("kind", "")
    body = notification.get("body", "")
    subject = _KIND_SUBJECTS.get(kind, f"New notification on {from_name}")

    text = f"{body}\n\nView it on your dashboard:\n{dashboard_url}"

    # SECURITY: `body` is user-controlled (e.g. a DM preview) and `dashboard_url`
    # is interpolated into an href — escape both before embedding in the HTML email,
    # or a crafted message body could inject markup into the recipient's mail client.
    safe_body = _html.escape(body)
    safe_url = _html.escape(dashboard_url, quote=True)
    html = (
        '<div style="font-family: -apple-system, Segoe UI, sans-serif; '
        'max-width: 480px; margin: 0 auto; padding: 24px; color: #1a1a1a;">'
        f'<p style="font-size: 15px;">{safe_body}</p>'
        '<p style="margin: 24px 0;">'
        f'<a href="{safe_url}" style="background: #2563eb; color: #ffffff; '
        'padding: 10px 20px; border-radius: 6px; text-decoration: none; '
        f'font-size: 14px; display: inline-block;">View dashboard</a></p>'
        '<p style="font-size: 13px; color: #666;">Or copy this link: '
        f'<a href="{safe_url}">{safe_url}</a></p>'
        "</div>"
    )

    return {"subject": subject, "text": text, "html": html}


def _render_telegram_text(notification: dict[str, Any], dashboard_url: str) -> str:
    kind = notification.get("kind", "notification")
    body = notification.get("body", "")
    return f"[Engram] {kind}: {body} — {dashboard_url}"


async def _send_email(
    settings: Any, recipient: Recipient, notification: dict[str, Any], http: Any
) -> dict[str, Any]:
    if not settings.cf_email_api_token or not recipient.email:
        return {"sent": False, "reason": "email off/no address/opted out"}
    if not recipient.prefs.get("email", True):
        return {"sent": False, "reason": "email opted out"}

    dashboard_url = f"{settings.public_url}/dashboard"
    body = build_notification_email(notification, settings.invite_from_name, dashboard_url)
    payload = {
        "to": recipient.email,
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

    try:
        resp = await http.post(url, json=payload, headers=headers)
    except Exception as exc:  # network error, timeout, etc.
        log.warning("notification email send failed for %s: %s", recipient.handle, exc)
        return {"sent": False, "reason": f"send failed: {exc}"}
    if resp.status_code // 100 != 2:
        log.warning(
            "notification email send to %s got status %s", recipient.handle, resp.status_code
        )
        return {"sent": False, "reason": f"Cloudflare returned status {resp.status_code}"}
    return {"sent": True, "to": recipient.email}


async def _send_telegram(
    settings: Any, recipient: Recipient, notification: dict[str, Any], http: Any
) -> dict[str, Any]:
    if not settings.telegram_bot_token or not recipient.telegram_chat_id:
        return {"sent": False, "reason": "telegram off/not linked/opted out"}
    if not recipient.prefs.get("telegram", True):
        return {"sent": False, "reason": "telegram opted out"}

    dashboard_url = f"{settings.public_url}/dashboard"
    text = _render_telegram_text(notification, dashboard_url)
    url = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {"chat_id": recipient.telegram_chat_id, "text": text}

    try:
        resp = await http.post(url, json=payload)
    except Exception as exc:  # network error, timeout, etc.
        log.warning("notification telegram send failed for %s: %s", recipient.handle, exc)
        return {"sent": False, "reason": f"send failed: {exc}"}
    if resp.status_code // 100 != 2:
        log.warning(
            "notification telegram send to %s got status %s", recipient.handle, resp.status_code
        )
        return {"sent": False, "reason": f"Telegram returned status {resp.status_code}"}
    return {"sent": True, "chat_id": recipient.telegram_chat_id}


async def fanout(
    settings: Any,
    recipient: Recipient,
    notification: dict[str, Any],
    *,
    http: Any | None = None,
) -> dict[str, Any]:
    """Deliver ``notification`` (``{"kind": str, "body": str, "ref": str|None}``,
    the shape social.py persists) to ``recipient`` over every enabled+configured
    channel. Never raises — every channel failure is caught and reported as its
    own ``{"sent": False, "reason": ...}`` entry so one channel's outage never
    blocks another.

    ``http`` is injectable (an httpx.AsyncClient or test fake exposing an async
    ``post(url, json=..., headers=...)``); tests pass a fake to intercept the
    call. When ``None``, a real client is opened for the actual sends.
    """
    dashboard_result = {
        "sent": True,
        "note": "in-app badge — the persisted notification row is the badge itself",
    }

    if http is not None:
        email_result = await _send_email(settings, recipient, notification, http)
        telegram_result = await _send_telegram(settings, recipient, notification, http)
    else:
        async with httpx.AsyncClient() as client:
            email_result = await _send_email(settings, recipient, notification, client)
            telegram_result = await _send_telegram(settings, recipient, notification, client)

    return {
        "channels": {
            "email": email_result,
            "telegram": telegram_result,
            "dashboard": dashboard_result,
        }
    }
