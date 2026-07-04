"""Pure presentation helpers: type glyphs, humanized time, properties panels.

No filesystem, no request state — every function here is deterministic and
unit-tested. Routes assemble these into pages.
"""

from __future__ import annotations

import datetime as dt

from engram_server.explorer.html import badge, chip, esc

# Unicode type glyphs (no icon fonts). Fallback for unknown/absent types.
TYPE_GLYPHS = {
    "decision": "⚖",
    "spec": "📐",
    "runbook": "🛠",
    "person": "👤",
    "client": "🏢",
    "video": "🎬",
    "message": "✉",
    "reference": "🔖",
    "project": "📁",
    "idea": "💡",
    "meeting": "🗓",
    "snippet": "✂",
    "note": "📝",
}
_DEFAULT_GLYPH = "◆"


def type_glyph(type_: object) -> str:
    """Unicode glyph for a frontmatter ``type`` (fallback ◆ for unknown)."""
    return TYPE_GLYPHS.get(str(type_ or "").strip().lower(), _DEFAULT_GLYPH)


def stamp(type_: object, *, small: bool = False) -> str:
    """The copper type-stamp: a glyph in a rounded accent-tinted square."""
    cls = "stamp-sm" if small else "stamp"
    return f'<span class="{cls}" aria-hidden="true">{esc(type_glyph(type_))}</span>'


# --------------------------------------------------------------- time


def _to_utc(value: object) -> dt.datetime | None:
    """Coerce a date/datetime/ISO-string to an aware UTC datetime, or None."""
    if isinstance(value, dt.datetime):
        d = value
    elif isinstance(value, dt.date):
        d = dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        d = None
        for candidate in (s, s[:19], s[:10]):
            try:
                d = dt.datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if d is None:
            return None
    else:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def humanize_time(value: object, now: dt.datetime | None = None) -> tuple[str, str]:
    """Return ``(relative, exact)`` for a timestamp, e.g. ('2 days ago', '2026-07-02').

    ``relative`` is a coarse human label ('just now', '3 months ago', 'in 2 days');
    ``exact`` is an ISO-ish absolute string for a title tooltip. Unparseable values
    round-trip unchanged so nothing is ever lost.
    """
    d = _to_utc(value)
    if d is None:
        text = str(value)
        return text, text
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    secs = (now - d).total_seconds()
    future = secs < 0
    secs = abs(secs)

    def unit(n: int, word: str) -> str:
        return f"1 {word}" if n == 1 else f"{n} {word}s"

    if secs < 45:
        rel = "just now"
    elif secs < 5400:  # < 1.5h
        rel = unit(max(1, round(secs / 60)), "minute")
    elif secs < 86400:  # < 1d
        rel = unit(round(secs / 3600), "hour")
    elif secs < 2592000:  # < 30d
        rel = unit(round(secs / 86400), "day")
    elif secs < 31104000:  # < 360d
        rel = unit(round(secs / 2592000), "month")
    else:
        rel = unit(round(secs / 31536000), "year")

    if rel != "just now":
        rel = f"in {rel}" if future else f"{rel} ago"

    if d.hour or d.minute:
        exact = d.strftime("%Y-%m-%d %H:%M UTC")
    else:
        exact = d.strftime("%Y-%m-%d")
    return rel, exact


def is_expired(expires: object, today: dt.date) -> bool:
    """True when a message ``expires`` value (date/datetime/str) is strictly past."""
    if isinstance(expires, dt.datetime):
        return expires.date() < today
    if isinstance(expires, dt.date):
        return expires < today
    if isinstance(expires, str):
        try:
            return dt.date.fromisoformat(expires.strip()[:10]) < today
        except ValueError:
            return False
    return False


# --------------------------------------------------------------- panels


def _time_pill(value: object, now: dt.datetime | None = None) -> str:
    rel, exact = humanize_time(value, now)
    return f'<span class="badge" title="{esc(exact)}">{esc(rel)}</span>'


def _prop(key: str, value_html: str) -> str:
    return f'<div class="prop"><span class="prop-k">{esc(key)}</span>' f'<span class="prop-v">{value_html}</span></div>'


def status_value(status: str) -> str:
    """A colored status dot + label (dot color keyed off the status word)."""
    s = str(status).strip().lower()
    return f'<span class="statusv"><span class="dot {esc(s)}"></span>{esc(status)}</span>'


def properties_panel(
    meta: dict,
    today: dt.date,
    *,
    now: dt.datetime | None = None,
    show_type: bool = True,
) -> str:
    """Notion-style key→value panel from a concept's frontmatter.

    Emits only the rows present in ``meta``; returns '' when nothing to show.
    Rows: type, status, confidence, project, to, priority, expires, tags, updated.
    """
    rows: list[str] = []
    if show_type and meta.get("type"):
        t = str(meta["type"])
        rows.append(_prop("Type", f'<span class="statusv">{stamp(t, small=True)}&nbsp;{esc(t)}</span>'))
    if meta.get("status"):
        rows.append(_prop("Status", status_value(str(meta["status"]))))
    if meta.get("confidence"):
        conf = str(meta["confidence"])
        cls = "superseded" if conf == "superseded" else "accent"
        rows.append(_prop("Confidence", badge(conf, cls)))
    if meta.get("project"):
        proj = str(meta["project"])
        rows.append(_prop("Project", chip(proj, f"/brain/p/{esc(proj)}")))
    if meta.get("to"):
        rows.append(_prop("To", badge(str(meta["to"]), "accent")))
    if meta.get("priority"):
        prio = str(meta["priority"])
        rows.append(_prop("Priority", badge(prio, "priority-high" if prio == "high" else "")))
    if meta.get("expires") is not None:
        expired = is_expired(meta["expires"], today)
        exp_html = badge(str(meta["expires"]), "superseded")
        if expired:
            exp_html += badge("expired", "priority-high")
        rows.append(_prop("Expires", exp_html))
    tags = meta.get("tags")
    if isinstance(tags, (list, tuple)) and tags:
        rows.append(_prop("Tags", "".join(chip(str(t), cls="tag") for t in tags)))
    elif isinstance(tags, str) and tags.strip():
        rows.append(_prop("Tags", chip(tags.strip(), cls="tag")))
    if meta.get("timestamp"):
        rows.append(_prop("Updated", _time_pill(meta["timestamp"], now)))
    if not rows:
        return ""
    return '<div class="props">' + "".join(rows) + "</div>"


def score_bar(score: float) -> str:
    """A copper progress track for a 0..1 search score, with a percent label."""
    pct = max(0, min(100, round(float(score) * 100)))
    return (
        '<div class="scorebar">'
        f'<span class="track"><span class="fill" style="width:{pct}%"></span></span>'
        f'<span class="pct">{pct}%</span>'
        "</div>"
    )
