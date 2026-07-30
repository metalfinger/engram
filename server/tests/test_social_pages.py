"""Unit tests for the social/discovery page bodies (v2 M5.4).

Pure functions, zero network, zero DB — just assert the fragment content, that
every attacker-controlled string is escaped, and that ``public_concept_body``
passes ``content_html`` through unescaped (it is caller-trusted).
"""

from engram_server.explorer.social_pages import (
    asks_body,
    feed_body,
    people_body,
    profile_body,
    public_concept_body,
)

XSS_SCRIPT = "<script>alert(1)</script>"
XSS_ATTR = '"><img src=x onerror=y>'


def assert_escaped(html: str, payload: str = XSS_SCRIPT) -> None:
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------- people_body

def test_people_body_empty():
    html = people_body([], "me")
    assert "Discover people" in html
    assert "No one to discover yet." in html


def test_people_body_renders_cards_and_follow_button():
    people = [
        {
            "handle": "alice",
            "display_name": "Alice A.",
            "avatar_url": "",
            "bio": "Builds things.",
            "followers": 3,
            "is_following": False,
            "public_projects": 2,
        },
        {
            "handle": "bob",
            "display_name": "Bob B.",
            "avatar_url": "https://example.com/bob.png",
            "bio": "",
            "followers": 1,
            "is_following": True,
            "public_projects": 0,
        },
    ]
    html = people_body(people, "me")
    assert "@alice" in html
    assert "Alice A." in html
    assert "Builds things." in html
    assert "2 public projects" in html
    assert "3 followers" in html
    assert "name='follow' value='1'" in html  # alice: not following -> Follow
    assert ">Follow<" in html
    assert "@bob" in html
    assert "https://example.com/bob.png" in html
    assert "name='follow' value='0'" in html  # bob: following -> Unfollow
    assert ">Unfollow<" in html
    assert "/dashboard/follow" in html


def test_people_body_omits_follow_for_viewer_own_row():
    people = [
        {"handle": "me", "display_name": "Me", "avatar_url": "", "bio": "", "followers": 0,
         "is_following": False, "public_projects": 0},
        {"handle": "alice", "display_name": "Alice", "avatar_url": "", "bio": "", "followers": 0,
         "is_following": False, "public_projects": 0},
    ]
    html = people_body(people, "me")
    # Split roughly at each card to confirm 'me' has no follow form.
    me_card = html.split("@me")[1].split("</div></div>", 1)[0]
    assert "/dashboard/follow" not in me_card
    assert "@alice" in html and "/dashboard/follow" in html


def test_people_body_escapes_xss():
    people = [{
        "handle": "evil",
        "display_name": XSS_SCRIPT,
        "avatar_url": "",
        "bio": XSS_ATTR,
        "followers": 0,
        "is_following": False,
        "public_projects": 0,
    }]
    html = people_body(people, "me")
    assert_escaped(html)
    assert '"><img src=x onerror=y>' not in html


# ---------------------------------------------------------------- profile_body

def test_profile_body_own_profile_omits_follow():
    profile = {
        "handle": "me", "display_name": "Me", "avatar_url": "", "bio": "hi",
        "followers": 5, "following": 2, "is_following": False,
    }
    html = profile_body(profile, [], "me")
    assert "/dashboard/follow" not in html
    assert "5 follower" in html
    assert "2 following" in html


def test_profile_body_renders_stats_and_public_work():
    profile = {
        "handle": "alice", "display_name": "Alice A.", "avatar_url": "", "bio": "Builds things.",
        "followers": 10, "following": 4, "is_following": False,
    }
    public_work = [
        {"path": "projects/foo/context.md", "title": "Foo context", "description": "About foo.",
         "type": "context", "project": "foo", "updated": "2026-07-01"},
    ]
    html = profile_body(profile, public_work, "me")
    assert "@alice" in html
    assert "Alice A." in html
    assert "Builds things." in html
    assert "10 follower" in html
    assert "4 following" in html
    assert "/dashboard/u/alice/f/projects/foo/context.md" in html
    assert "Foo context" in html
    assert "About foo." in html
    assert "foo" in html
    assert "name='follow' value='1'" in html  # not following viewer's own -> Follow
    assert "/dashboard/follow" in html


def test_profile_body_empty_work():
    profile = {"handle": "alice", "display_name": "", "avatar_url": "", "bio": "",
               "followers": 0, "following": 0, "is_following": False}
    html = profile_body(profile, [], "me")
    assert "@alice hasn't published anything yet." in html


def test_profile_body_escapes_xss():
    profile = {
        "handle": "alice", "display_name": XSS_SCRIPT, "avatar_url": "", "bio": XSS_ATTR,
        "followers": 0, "following": 0, "is_following": False,
    }
    public_work = [{"path": "p", "title": XSS_SCRIPT, "description": XSS_ATTR, "type": "note",
                     "project": "p", "updated": ""}]
    html = profile_body(profile, public_work, "me")
    assert_escaped(html)
    assert '"><img src=x onerror=y>' not in html


# ---------------------------------------------------------------- feed_body

def test_feed_body_empty():
    html = feed_body([])
    assert "Nothing yet — follow some people to see their public work here." in html


def test_feed_body_renders_items():
    items = [
        {"handle": "alice", "display_name": "Alice A.", "avatar_url": "", "path": "projects/foo/idea.md",
         "title": "New idea", "description": "A neat idea.", "project": "foo", "updated": "2026-07-29"},
    ]
    html = feed_body(items)
    assert "/dashboard/u/alice/f/projects/foo/idea.md" in html
    assert "New idea" in html
    assert "Alice A." in html
    assert "@alice" in html
    assert "A neat idea." in html
    assert "foo" in html
    assert "2026-07-29" in html


def test_feed_body_escapes_xss():
    items = [
        {"handle": "alice", "display_name": XSS_SCRIPT, "avatar_url": "", "path": "p",
         "title": XSS_SCRIPT, "description": XSS_ATTR, "project": "", "updated": ""},
    ]
    html = feed_body(items)
    assert_escaped(html)
    assert '"><img src=x onerror=y>' not in html


# ---------------------------------------------------------------- asks_body

def test_asks_body_empty_states():
    html = asks_body([], [], "me")
    assert "No one has asked you anything yet." in html
    assert "You haven't asked anything yet." in html


def test_asks_body_to_answer_open_shows_form():
    to_answer = [{
        "id": "42", "from_handle": "alice", "to_handle": "me", "path": "projects/foo/context.md",
        "question": "What's next?", "answer": "", "status": "open", "created": "2026-07-29",
        "answered_at": "",
    }]
    html = asks_body(to_answer, [], "me")
    assert "@alice asked about" in html
    assert "/dashboard/f/projects/foo/context.md" in html
    assert "What&#x27;s next?" in html  # esc() escapes apostrophes too
    assert "name='ask_id' value='42'" in html
    assert "/dashboard/asks/answer" in html
    assert "<textarea name='answer'" in html


def test_asks_body_to_answer_answered_shows_answer_no_form():
    to_answer = [{
        "id": "43", "from_handle": "alice", "to_handle": "me", "path": "p",
        "question": "Q?", "answer": "It's done.", "status": "answered", "created": "", "answered_at": "2026-07-30",
    }]
    html = asks_body(to_answer, [], "me")
    assert "It&#x27;s done." in html  # esc() escapes apostrophes too
    assert "/dashboard/asks/answer" not in html


def test_asks_body_i_asked_waiting_and_answered():
    i_asked = [
        {"id": "1", "from_handle": "me", "to_handle": "bob", "path": "projects/x/context.md",
         "question": "Ping?", "answer": "", "status": "open", "created": "", "answered_at": ""},
        {"id": "2", "from_handle": "me", "to_handle": "carol", "path": "y",
         "question": "Q2?", "answer": "A2.", "status": "answered", "created": "", "answered_at": ""},
    ]
    html = asks_body([], i_asked, "me")
    assert "/dashboard/u/bob/f/projects/x/context.md" in html
    assert "Ping?" in html
    assert "waiting for an answer" in html
    assert "/dashboard/u/carol/f/y" in html
    assert "A2." in html


def test_asks_body_escapes_xss():
    to_answer = [{
        "id": "1", "from_handle": XSS_SCRIPT, "to_handle": "me", "path": "p",
        "question": XSS_SCRIPT, "answer": "", "status": "open", "created": "", "answered_at": "",
    }]
    i_asked = [{
        "id": "2", "from_handle": "me", "to_handle": XSS_SCRIPT, "path": "p",
        "question": XSS_ATTR, "answer": XSS_SCRIPT, "status": "answered", "created": "", "answered_at": "",
    }]
    html = asks_body(to_answer, i_asked, "me")
    assert_escaped(html)
    assert '"><img src=x onerror=y>' not in html


# ---------------------------------------------------------------- public_concept_body

def test_public_concept_body_renders():
    html = public_concept_body(
        "alice", "projects/foo/context.md", "Foo context",
        {"type": "context"}, "<p>Trusted rendered markdown.</p>", "me",
    )
    assert "Foo context" in html
    assert "context" in html
    assert "/dashboard/u/alice" in html
    assert "@alice" in html
    assert "<p>Trusted rendered markdown.</p>" in html
    assert "Ask @alice about this" in html
    assert "/dashboard/ask" in html
    assert "name='handle' value='alice'" in html
    assert "name='path' value='projects/foo/context.md'" in html


def test_public_concept_body_content_html_passes_through_unescaped():
    trusted = "<p>Has a <b>bold</b> word and an <a href='/dashboard/f/x'>internal link</a>.</p>"
    html = public_concept_body("alice", "p", "T", {}, trusted, "me")
    assert trusted in html


def test_public_concept_body_escapes_everything_but_content_html():
    html = public_concept_body(
        XSS_SCRIPT, "p", XSS_SCRIPT, {"type": XSS_SCRIPT},
        "<p>trusted</p>", "me",
    )
    assert_escaped(html)
    assert "<p>trusted</p>" in html
