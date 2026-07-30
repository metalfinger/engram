"""Social/discovery page BODIES (v2 M5.4) — pure render functions, zero routing.

Each function returns an HTML *fragment* (the ``<main>`` body only); the dashboard
wraps it in ``_brain_shell`` and owns every route (``dashboard.py``). This file owns
nothing but rendering, so it can be developed/tested collision-free alongside the
route wiring.

Same house style as the rest of the explorer: server-rendered, zero JavaScript,
the shared CSS vocabulary from ``engram_server.explorer.html`` (``card``, ``cards``,
``page-head``, ``eyebrow``, ``timeline``/``tl-item``, ``section-label``, ``empty``,
``stat-row``, ``meta``, ``desc``).

Security: every interpolated value is attacker-controlled text (display name, bio,
question, answer, title, handle, path) EXCEPT ``content_html`` in
``public_concept_body``, which the caller has already rendered from trusted
markdown — it is inserted as-is. Everything else goes through ``esc()``, including
values placed in hidden form inputs and hrefs.
"""

from __future__ import annotations

from engram_server.explorer.html import badge, chip, esc


def _avatar(avatar_url: str, name: str, size: int = 40) -> str:
    """A small <img> for an https avatar, or a letter-circle fallback."""
    if avatar_url:
        return (
            f"<img src='{esc(avatar_url)}' width={size} height={size} alt='' "
            "style='border-radius:50%;object-fit:cover;vertical-align:middle'>"
        )
    letter = esc(((name or "").strip()[:1] or "?").upper())
    return (
        f"<span style='display:inline-flex;width:{size}px;height:{size}px;border-radius:50%;"
        "background:var(--accent);color:var(--accent-fg);align-items:center;justify-content:center;"
        f"font-weight:600;vertical-align:middle'>{letter}</span>"
    )


def _follow_form(handle: str, is_following: bool) -> str:
    label = "Unfollow" if is_following else "Follow"
    value = "0" if is_following else "1"
    return (
        "<form method='post' action='/dashboard/follow'>"
        f"<input type='hidden' name='handle' value='{esc(handle)}'>"
        f"<input type='hidden' name='follow' value='{value}'>"
        f"<button type='submit'>{label}</button></form>"
    )


def people_body(people: list[dict], viewer_handle: str) -> str:
    """Discover-people grid: avatar, handle, bio, stats, and a Follow/Unfollow form."""
    parts = ["<div class='page-head'><div><p class='eyebrow'>Discover</p><h1>Discover people</h1></div></div>"]
    if not people:
        parts.append("<p class='empty'>No one to discover yet.</p>")
        return "".join(parts)
    cards = []
    for p in people:
        handle = str(p.get("handle") or "")
        display_name = str(p.get("display_name") or "")
        avatar_url = str(p.get("avatar_url") or "")
        bio = str(p.get("bio") or "")
        followers = int(p.get("followers") or 0)
        public_projects = int(p.get("public_projects") or 0)
        is_following = bool(p.get("is_following"))
        btn = "" if handle == viewer_handle else _follow_form(handle, is_following)
        cards.append(
            "<div class='card'>"
            f"<div class='card-head'>{_avatar(avatar_url, display_name or handle)}"
            f"<div><h3>@{esc(handle)}</h3>"
            + (f"<p class='meta'>{esc(display_name)}</p>" if display_name else "")
            + "</div></div>"
            + (f"<p class='desc'>{esc(bio)}</p>" if bio else "")
            + f"<p class='meta'>{public_projects} public project{'s' if public_projects != 1 else ''} · "
            f"{followers} follower{'s' if followers != 1 else ''}</p>"
            + f"<div class='card-foot'>{btn}</div>"
            "</div>"
        )
    parts.append(f"<div class='cards'>{''.join(cards)}</div>")
    return "".join(parts)


def profile_body(profile: dict, public_work: list[dict], viewer_handle: str) -> str:
    """Big-avatar page-head + follower/following stats + a public-work card grid."""
    handle = str(profile.get("handle") or "")
    display_name = str(profile.get("display_name") or "")
    avatar_url = str(profile.get("avatar_url") or "")
    bio = str(profile.get("bio") or "")
    followers = int(profile.get("followers") or 0)
    following = int(profile.get("following") or 0)
    is_following = bool(profile.get("is_following"))

    head = (
        "<div class='page-head'>"
        f"{_avatar(avatar_url, display_name or handle, size=64)}"
        "<div>"
        f"<p class='eyebrow'>@{esc(handle)}</p>"
        f"<h1>{esc(display_name) if display_name else '@' + esc(handle)}</h1>"
        + (f"<p class='desc'>{esc(bio)}</p>" if bio else "")
        + "</div></div>"
    )
    stats = (
        "<div class='stat-row'>"
        + badge(f"{followers} follower{'s' if followers != 1 else ''}")
        + badge(f"{following} following")
        + "</div>"
    )
    follow_form = "" if handle == viewer_handle else _follow_form(handle, is_following)

    parts = [head, stats, follow_form, "<p class='section-label'>Public work</p>"]
    if not public_work:
        parts.append(f"<p class='empty'>@{esc(handle)} hasn't published anything yet.</p>")
    else:
        cards = []
        for w in public_work:
            path = str(w.get("path") or "")
            title = str(w.get("title") or path.rsplit("/", 1)[-1])
            desc = str(w.get("description") or "")
            project = str(w.get("project") or "")
            updated = str(w.get("updated") or "")
            foot = (chip(project) if project else "") + (f"<span class='meta'>{esc(updated)}</span>" if updated else "")
            cards.append(
                f"<a class='card' href='/dashboard/u/{esc(handle)}/f/{esc(path)}'>"
                f"<h3>{esc(title)}</h3>"
                + (f"<p class='desc'>{esc(desc)}</p>" if desc else "")
                + f"<div class='card-foot'>{foot}</div>"
                "</a>"
            )
        parts.append(f"<div class='cards'>{''.join(cards)}</div>")
    return "".join(parts)


def feed_body(items: list[dict]) -> str:
    """Newest-first timeline of published work across everyone the viewer follows."""
    parts = ["<div class='page-head'><div><p class='eyebrow'>Feed</p><h1>Feed</h1></div></div>"]
    if not items:
        parts.append("<p class='empty'>Nothing yet — follow some people to see their public work here.</p>")
        return "".join(parts)
    entries = []
    for it in items:
        handle = str(it.get("handle") or "")
        display_name = str(it.get("display_name") or "")
        avatar_url = str(it.get("avatar_url") or "")
        path = str(it.get("path") or "")
        title = str(it.get("title") or path.rsplit("/", 1)[-1])
        desc = str(it.get("description") or "")
        project = str(it.get("project") or "")
        updated = str(it.get("updated") or "")
        who = display_name or f"@{handle}"
        entries.append(
            "<div class='tl-item'>"
            f"<span class='tl-date'>{esc(updated)}</span>"
            f"<h3>{_avatar(avatar_url, who, size=20)} "
            f"<a href='/dashboard/u/{esc(handle)}/f/{esc(path)}'>{esc(title)}</a></h3>"
            "<div class='tl-body'>"
            f"<p>{esc(who)} (@{esc(handle)}) published"
            + (f" in {esc(project)}" if project else "")
            + (f" — {esc(desc)}" if desc else "")
            + "</p></div></div>"
        )
    parts.append(f"<div class='timeline'>{''.join(entries)}</div>")
    return "".join(parts)


def asks_body(to_answer: list[dict], i_asked: list[dict], viewer_handle: str) -> str:
    """Two sections: open questions on the viewer's own concepts, and questions the
    viewer sent to others."""
    parts = ["<div class='page-head'><div><p class='eyebrow'>Asks</p><h1>Asks</h1></div></div>"]

    parts.append("<p class='section-label'>Questions for you</p>")
    if not to_answer:
        parts.append("<p class='empty'>No one has asked you anything yet.</p>")
    else:
        cards = []
        for a in to_answer:
            ask_id = str(a.get("id") or "")
            from_handle = str(a.get("from_handle") or "")
            path = str(a.get("path") or "")
            question = str(a.get("question") or "")
            answer = str(a.get("answer") or "")
            status = str(a.get("status") or "")
            created = str(a.get("created") or "")
            body = (
                "<div class='card'>"
                f"<p class='meta'>@{esc(from_handle)} asked about "
                f"<a href='/dashboard/f/{esc(path)}'>{esc(path)}</a>"
                + (f" · {esc(created)}" if created else "")
                + "</p>"
                f"<p class='desc'>{esc(question)}</p>"
            )
            if status == "open":
                body += (
                    "<form method='post' action='/dashboard/asks/answer'>"
                    f"<input type='hidden' name='ask_id' value='{esc(ask_id)}'>"
                    "<textarea name='answer' required></textarea>"
                    "<button type='submit'>Answer</button></form>"
                )
            else:
                body += f"<p class='meta'><b>Answer:</b> {esc(answer)}</p>"
            body += "</div>"
            cards.append(body)
        parts.append(f"<div class='cards'>{''.join(cards)}</div>")

    parts.append("<p class='section-label'>You asked</p>")
    if not i_asked:
        parts.append("<p class='empty'>You haven't asked anything yet.</p>")
    else:
        cards = []
        for a in i_asked:
            to_handle = str(a.get("to_handle") or "")
            path = str(a.get("path") or "")
            question = str(a.get("question") or "")
            answer = str(a.get("answer") or "")
            status = str(a.get("status") or "")
            body = (
                "<div class='card'>"
                f"<p class='meta'>To @{esc(to_handle)} about "
                f"<a href='/dashboard/u/{esc(to_handle)}/f/{esc(path)}'>{esc(path)}</a></p>"
                f"<p class='desc'>{esc(question)}</p>"
            )
            if status == "open":
                body += "<p class='empty'>waiting for an answer…</p>"
            else:
                body += f"<p class='meta'><b>Answer:</b> {esc(answer)}</p>"
            body += "</div>"
            cards.append(body)
        parts.append(f"<div class='cards'>{''.join(cards)}</div>")

    return "".join(parts)


def public_concept_body(handle: str, path: str, title: str, meta: dict, content_html: str, viewer_handle: str) -> str:
    """A shared concept: page-head, byline back to the owner's profile, the
    (already-rendered, trusted) markdown body, and an Ask form.

    ``content_html`` is inserted verbatim — the caller is responsible for having
    rendered it safely from markdown. Every other value here is escaped.
    """
    eyebrow = str((meta or {}).get("type") or "concept")
    head = (
        "<div class='page-head'><div>"
        f"<p class='eyebrow'>{esc(eyebrow)}</p><h1>{esc(title)}</h1>"
        f"<p class='meta'>by <a href='/dashboard/u/{esc(handle)}'>@{esc(handle)}</a></p>"
        "</div></div>"
    )
    ask = (
        "<p class='section-label'>Ask</p>"
        "<form method='post' action='/dashboard/ask'>"
        f"<input type='hidden' name='handle' value='{esc(handle)}'>"
        f"<input type='hidden' name='path' value='{esc(path)}'>"
        f"<textarea name='question' placeholder='Ask @{esc(handle)} about this' required></textarea>"
        "<button type='submit'>Ask</button></form>"
    )
    return head + f"<div class='md'>{content_html}</div>" + ask
