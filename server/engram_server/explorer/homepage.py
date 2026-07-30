"""Public marketing homepage (``GET /``).

Unlike every other route in ``explorer/``, this one carries NO auth guard —
it is the front door an invited colleague hits before they have a brain, an
OAuth login, or a dashboard session. Self-contained on purpose: no external
CSS/JS/fonts, so it renders identically behind any tunnel or CDN and costs
nothing to serve.
"""

from __future__ import annotations

from html import escape as _escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from engram_server.config import Settings

CSS = """\
:root {
  --bg: #faf7f2; --surface: #ffffff; --surface-2: #f4efe6; --inset: #f7f2ea;
  --fg: #221f1a; --muted: #7a7266; --faint: #a89e8e;
  --line: #eae1d4; --line-2: #e0d6c4;
  --accent: #b5622b; --accent-ink: #9c4f1f; --accent-fg: #ffffff;
  --accent-soft: #f1e3d3; --accent-line: #e3c9ad;
  --code-bg: #221f1a; --code-fg: #f4efe6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16130d; --surface: #1e1a13; --surface-2: #241f16; --inset: #211c14;
    --fg: #ece6da; --muted: #a09784; --faint: #6d6453;
    --line: #2c261b; --line-2: #39321f;
    --accent: #d98a4e; --accent-ink: #e29a63; --accent-fg: #1a1409;
    --accent-soft: rgba(217,138,78,.13); --accent-line: #4a3820;
    --code-bg: #0f0d09; --code-fg: #ece6da;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  font-size: 16px; line-height: 1.65; letter-spacing: -0.003em;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent-ink); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, Menlo, monospace; font-size: .92em; }

.wrap { max-width: 62rem; margin: 0 auto; padding: 0 1.25rem; }

.topbar {
  border-bottom: 1px solid var(--line);
  padding: 1.1rem 0;
}
.topbar-inner { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.wordmark {
  font-weight: 700; font-size: 1.15rem; letter-spacing: -0.02em; color: var(--fg);
  display: inline-flex; align-items: center; gap: .5rem;
}
.wordmark .dot { color: var(--accent); }
a.cta {
  display: inline-block;
  border: 1px solid var(--line-2); border-radius: .6rem; padding: .45rem .9rem;
  font-size: .92rem; font-weight: 600; color: var(--fg); white-space: nowrap;
}
a.cta:hover { background: var(--surface-2); text-decoration: none; }
/* The primary call to action: creating an account. */
a.cta.primary {
  background: var(--accent); border-color: var(--accent); color: var(--accent-fg);
  padding: .6rem 1.15rem; font-size: 1rem;
}
a.cta.primary:hover { background: var(--accent-ink); border-color: var(--accent-ink); }

.hero { padding: 4.5rem 0 3rem; }
.hero h1 {
  font-size: clamp(1.9rem, 4.5vw, 3rem); line-height: 1.12; letter-spacing: -0.02em;
  margin: 0 0 1.1rem; max-width: 34ch;
}
.hero p.lede { font-size: 1.15rem; color: var(--muted); max-width: 44ch; margin: 0 0 2rem; }
.hero .actions { display: flex; flex-wrap: wrap; gap: .75rem; }
.btn {
  display: inline-flex; align-items: center; gap: .4rem; border-radius: .7rem;
  padding: .7rem 1.3rem; font-weight: 600; font-size: .98rem; border: 1px solid transparent;
}
.btn-primary { background: var(--accent); color: var(--accent-fg); }
.btn-primary:hover { background: var(--accent-ink); text-decoration: none; }
.btn-ghost { border-color: var(--line-2); color: var(--fg); }
.btn-ghost:hover { background: var(--surface-2); text-decoration: none; }

section { padding: 2.75rem 0; border-top: 1px solid var(--line); }
section > h2 {
  font-size: 1.5rem; letter-spacing: -0.01em; margin: 0 0 .4rem;
}
section > p.section-lede { color: var(--muted); margin: 0 0 1.75rem; max-width: 52ch; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: 1rem; }
.card {
  background: var(--surface); border: 1px solid var(--line); border-radius: .9rem;
  padding: 1.25rem 1.35rem;
}
.card .glyph {
  width: 2.1rem; height: 2.1rem; border-radius: .55rem; background: var(--accent-soft);
  display: flex; align-items: center; justify-content: center; font-size: 1.1rem;
  margin-bottom: .7rem;
}
.card h3 { font-size: 1.02rem; margin: 0 0 .35rem; letter-spacing: -0.01em; }
.card p { margin: 0; color: var(--muted); font-size: .94rem; }

.clients { display: grid; gap: 1.1rem; }
.client {
  background: var(--surface); border: 1px solid var(--line); border-radius: .9rem;
  padding: 1.25rem 1.35rem;
}
.client h3 { margin: 0 0 .6rem; font-size: 1.02rem; display: flex; align-items: center; gap: .5rem; }
.client ol { margin: 0 0 .9rem; padding-left: 1.2rem; color: var(--muted); font-size: .94rem; }
.client ol li { margin: .2rem 0; }
.codebox {
  background: var(--code-bg); color: var(--code-fg); border-radius: .6rem;
  padding: .8rem 1rem; overflow-x: auto; white-space: pre; font-size: .88rem;
  -webkit-overflow-scrolling: touch;
}
.codebox .prompt { opacity: .55; }

.note {
  background: var(--surface-2); border: 1px solid var(--line-2); border-radius: .9rem;
  padding: 1.1rem 1.3rem; font-size: .95rem; color: var(--muted);
}
.note strong { color: var(--fg); }

footer {
  border-top: 1px solid var(--line); padding: 2.25rem 0 3rem;
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 1rem;
}
footer .wordmark { font-size: .95rem; }
footer a { color: var(--muted); font-size: .92rem; }
footer a:hover { color: var(--fg); }

@media (max-width: 40rem) {
  .hero { padding: 3rem 0 2rem; }
  section { padding: 2rem 0; }
}
"""


def _card(glyph: str, title: str, body: str) -> str:
    return (
        '<div class="card">'
        f'<div class="glyph">{glyph}</div>'
        f"<h3>{_escape(title)}</h3>"
        f"<p>{body}</p>"
        "</div>"
    )


def _client_block(title: str, glyph: str, steps: list[str], code: str | None) -> str:
    items = "".join(f"<li>{step}</li>" for step in steps)
    code_html = f'<div class="codebox">{_escape(code)}</div>' if code else ""
    return (
        '<div class="client">'
        f"<h3>{glyph} {_escape(title)}</h3>"
        f"<ol>{items}</ol>"
        f"{code_html}"
        "</div>"
    )


def homepage_html(settings: Settings) -> str:
    """Render the self-contained public homepage (zero external requests)."""
    mcp_url = settings.public_url.rstrip("/") + "/mcp"
    dashboard_url = settings.public_url.rstrip("/") + "/dashboard"
    signup_url = settings.public_url.rstrip("/") + "/join"
    mcp_url_esc = _escape(mcp_url)

    cards = "".join(
        [
            _card(
                "&#128172;",
                "Pick up where you left off",
                "Start a new session on any device and your AI already knows the project — "
                "context, decisions, and what's still open, no re-explaining.",
            ),
            _card(
                "&#128203;",
                "Capture it once, recall it anywhere",
                "Decisions, specs, and runbooks get written to your brain a single time and "
                "come back whenever a session needs them — this device or the next one.",
            ),
            _card(
                "&#9993;",
                "Leave a note for your future self",
                "Wrap up a session with a message for whichever session opens next, on "
                "whichever machine — it's waiting when that session starts.",
            ),
            _card(
                "&#129309;",
                "Share it with your team",
                "Invite a teammate and their AI can read from — and write into — the same "
                "knowledge base, so their sessions answer from what your team already knows.",
            ),
        ]
    )

    claude_ai_steps = [
        "Open <strong>Settings &rarr; Connectors &rarr; Add custom connector</strong>.",
        f"Paste the MCP URL: <code>{mcp_url_esc}</code>",
        "Sign in when prompted &mdash; that's it, the connector is live.",
    ]
    claude_code_steps = [
        "Open a terminal in any project.",
        "Run the command below to register Engram as an MCP server.",
        "Claude Code will prompt you to sign in on first use.",
    ]
    chatgpt_steps = [
        "Open <strong>Settings &rarr; Connectors</strong> and add a custom MCP connector.",
        f"Paste the same MCP URL: <code>{mcp_url_esc}</code>",
        "Requires a ChatGPT plan that supports custom MCP connectors.",
    ]

    clients = "".join(
        [
            _client_block("claude.ai / Claude mobile", "&#127760;", claude_ai_steps, None),
            _client_block(
                "Claude Code (terminal)",
                "&#9000;",
                claude_code_steps,
                f"$ claude mcp add --transport http engram {mcp_url}",
            ),
            _client_block("ChatGPT", "&#128172;", chatgpt_steps, None),
        ]
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Engram &mdash; your AI's long-term memory</title>
<meta name="description" content="Engram is a private, git-backed knowledge base your Claude or ChatGPT can read and write across every session and device.">
<style>{CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="wrap topbar-inner">
    <span class="wordmark">Engram<span class="dot">.</span></span>
    <a class="cta primary" href="{_escape(signup_url)}">Create your Engram</a>
  </div>
</header>

<main>
  <section class="hero wrap" style="border-top: none;">
    <h1>Your AI's long-term memory.</h1>
    <p class="lede">
      Engram is a private, git-backed knowledge base your Claude or ChatGPT can read and
      write across every session and every device &mdash; so nothing you've already figured
      out has to be re-explained.
    </p>
    <div class="actions">
      <a class="btn btn-primary" href="#connect">See how to connect</a>
      <a class="btn btn-ghost" href="{_escape(dashboard_url)}">Open your dashboard</a>
    </div>
  </section>

  <section class="wrap" id="what">
    <h2>What you can do</h2>
    <p class="section-lede">One brain, read and written by every AI session you open.</p>
    <div class="cards">{cards}</div>
  </section>

  <section class="wrap" id="connect">
    <h2>How to connect</h2>
    <p class="section-lede">
      Same brain, any client &mdash; connect once per app, then every new session on that
      app can reach it.
    </p>
    <div class="clients">{clients}</div>
  </section>

  <section class="wrap" id="private">
    <h2>Free, and private by default</h2>
    <div class="note">
      <p style="margin:0 0 .6rem;">
        <strong>Anyone can create one.</strong> Sign in with GitHub or Google &mdash; no
        password to manage or lose, and nothing to pay.
      </p>
      <p style="margin:0 0 .6rem;">
        <strong>Everything starts private.</strong> Nothing you write is visible to anyone
        until you explicitly publish it &mdash; and you can see exactly what's public at a glance.
      </p>
      <p style="margin:0;">
        <strong>Your brain is yours.</strong> It's a real git repository, so it's portable:
        nothing about your knowledge base is locked to Engram.
      </p>
    </div>
    <p style="margin:1.4rem 0 0;">
      <a class="cta primary" href="{_escape(signup_url)}">Create your Engram &rarr;</a>
      <a class="cta" href="{_escape(dashboard_url)}" style="margin-left:.5rem">Sign in</a>
    </p>
  </section>
</main>

<footer class="wrap">
  <span class="wordmark">Engram<span class="dot">.</span></span>
  <a href="{_escape(dashboard_url)}">Already have one? Open your dashboard</a>
</footer>
</body>
</html>
"""


def register_homepage(mcp, settings: Settings) -> None:
    """Register the public ``GET /`` marketing homepage.

    Deliberately UNGUARDED (no Cloudflare Access / OAuth check) — this is the
    page an invited colleague sees before they have any credentials at all.
    """

    @mcp.custom_route("/", ["GET"])
    async def homepage_view(request: Request) -> Response:
        return HTMLResponse(homepage_html(settings))
