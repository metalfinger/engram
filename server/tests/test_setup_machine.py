"""kb_setup_machine — a new computer set up from one pasted prompt.

Hiren works across three machines and adds more. Setting one up has meant being
walked through curl commands, and the macOS hook fix had to be hand-delivered by
commit-pinned URL. The session on the new machine can do all of it: it has file
access, and the server already knows who it is.
"""

from types import SimpleNamespace

import pytest

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
    inv = registry.tenancy.create_invite("alice@example.com")
    # GITHUB, deliberately — like Hiren's real account. A Google fixture would let
    # a token minted as "google:<email>" resolve by coincidence and hide the bug
    # that made every machine token 401 in the field.
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "github", "github:alice")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="github:alice"))
    return registry


@pytest.mark.asyncio
async def test_setup_returns_everything_without_asking_the_user(mu):
    out = await app_module.kb_setup_machine()
    assert out["server_url"]
    assert {s["name"] for s in out["skills"]} >= {"engram", "chunk-work"}
    assert out["hooks"]["zip"].endswith("engram-hooks.zip")
    assert out["upload_config"]["path"] == "~/.engram/upload.json"
    assert len(out["steps"]) >= 5


@pytest.mark.asyncio
async def test_it_mints_a_token_so_nobody_pastes_a_secret(mu):
    """The session is already authenticated with full read/write to the brain, so a
    notify-scope token is strictly LESS power — handing it over is not escalation."""
    out = await app_module.kb_setup_machine()
    assert out["token"], "without this the user is back to copying secrets by hand"
    assert out["upload_config"]["contents"]["token"] == out["token"]
    assert out["upload_config"]["contents"]["url"] == out["server_url"]


@pytest.mark.asyncio
async def test_the_settings_snippet_covers_the_three_hook_events(mu):
    out = await app_module.kb_setup_machine()
    hooks = out["settings_snippet"]["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}


@pytest.mark.asyncio
async def test_it_insists_on_verifying_rather_than_assuming(mu):
    """The hooks always exit 0, so a broken install looks exactly like a working
    one. That cost an afternoon once already."""
    out = await app_module.kb_setup_machine()
    steps = " ".join(out["steps"])
    assert "VERIFY" in steps and "kb_roster" in steps
    assert "exit 0" in steps


@pytest.mark.asyncio
async def test_it_warns_that_a_file_copy_cannot_refresh_tool_schemas(mu):
    out = await app_module.kb_setup_machine()
    assert "/mcp" in " ".join(out["steps"])


@pytest.mark.asyncio
async def test_it_says_to_append_hooks_not_replace_them(mu):
    """Other tools' hooks live in the same file; replacing the block would silently
    disable them."""
    out = await app_module.kb_setup_machine()
    assert "APPEND" in " ".join(out["steps"])


@pytest.mark.asyncio
async def test_a_failure_to_mint_still_returns_usable_setup(mu, monkeypatch):
    """Presence upload is one part of setup; losing it must not block the rest."""
    class _Broken:
        class auth:
            @staticmethod
            def issue(*a, **k):
                raise RuntimeError("no signing key")

    monkeypatch.setattr(app_module, "_dashboard", _Broken)
    out = await app_module.kb_setup_machine()
    assert out["token"] == ""
    assert out["skills"], "the rest of setup still works"
    assert "ask Hiren" in out["upload_config"]["contents"]["token"]


@pytest.mark.asyncio
async def test_setup_is_also_the_update_check(mu):
    """Setup and update are one call on purpose. A separate 'check for updates'
    command is one more thing to remember, and the thing nobody remembers is what
    leaves a machine stale for six weeks."""
    out = await app_module.kb_setup_machine()
    steps = " ".join(out["steps"])
    assert "CHECK FIRST" in steps
    assert "already current" in steps and "do not re-download" in steps
    assert out["local_digest_recipe"], "a client cannot compare without the recipe"
    assert any("RUN THIS AGAIN" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_the_digest_recipe_matches_what_the_server_actually_does(mu, tmp_path):
    """If the recipe drifts from _dir_digest, every machine reports itself stale
    forever — which is worse than no check at all, because it trains you to ignore
    it."""
    import hashlib

    from engram_server.app import _dir_digest

    d = tmp_path / "sk"
    (d / "sub").mkdir(parents=True)
    (d / "SKILL.md").write_text("alpha", encoding="utf-8")
    (d / "sub" / "extra.md").write_text("beta", encoding="utf-8")

    h = hashlib.sha256()
    for p in sorted(x for x in d.rglob("*") if x.is_file()):
        h.update(p.relative_to(d).as_posix().encode("utf-8"))
        h.update(p.read_bytes())
    assert _dir_digest(d) == h.hexdigest()[:12]


@pytest.mark.asyncio
async def test_setup_offers_the_status_line(mu):
    """The status line is the zero-token surface: everything it shows is knowable
    through tools, but a tool result costs context and terminal chrome does not."""
    out = await app_module.kb_setup_machine()
    assert out["settings_snippet"]["statusLine"]["command"].endswith(
        "engram-statusline.sh")
    assert "costs nothing" in out["status_line"]["why"]


@pytest.mark.asyncio
async def test_it_refuses_to_clobber_an_existing_status_line(mu):
    """A status line is personal — his already carried model, git, context and token
    counts. Replacing it silently would be the rudest thing setup could do, so the
    answer is to WRAP: his command runs first, ours prints beneath it."""
    out = await app_module.kb_setup_machine()
    assert "DO NOT replace" in out["status_line"]["if_one_exists"]
    assert "OPTIONAL" in " ".join(out["steps"])
    merge = out["status_line"]["merge"]
    assert merge["path"] == "~/.engram/statusline.json"
    assert "parent" in merge["contents"]
    assert merge["undo"]


@pytest.mark.asyncio
async def test_it_says_to_skip_upload_config_on_the_server_machine(mu):
    """Uploading from the server's own box is a pointless localhost round trip AND
    strictly worse: a spooled record survives the server being down and is ingested
    when it returns, whereas a failed upload is dropped by design."""
    out = await app_module.kb_setup_machine()
    skip = out["upload_config"]["skip_if"]
    assert "~/.engram/brain" in skip
    assert "dropped" in skip


@pytest.mark.asyncio
async def test_the_hooks_digest_ignores_other_tools_hooks(mu, tmp_path):
    """The bug this pins: ~/.claude/hooks/ is SHARED. On Hiren's PC it also holds
    helix and anatomy hooks and a __pycache__, so a whole-directory digest could
    never equal the server's and the update check said "stale" on every run —
    a signal that is always red is a signal nobody reads."""
    theirs = tmp_path / "installed"
    theirs.mkdir()
    (theirs / "engram_presence_hook.py").write_text("A", encoding="utf-8")
    (theirs / "engram-statusline.sh").write_text("B", encoding="utf-8")

    ours = tmp_path / "repo"
    ours.mkdir()
    (ours / "engram_presence_hook.py").write_text("A", encoding="utf-8")
    (ours / "engram-statusline.sh").write_text("B", encoding="utf-8")
    (ours / "README.md").write_text("install docs", encoding="utf-8")

    # Same Engram files, wildly different directories.
    (theirs / "helix-post-tool.sh").write_text("someone else's hook", encoding="utf-8")
    (theirs / "__pycache__").mkdir()
    (theirs / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")

    assert app_module._dir_digest(theirs, app_module._owned_hooks(theirs)) == \
        app_module._dir_digest(ours, app_module._owned_hooks(ours))
    # ...and the naive whole-tree digest is exactly what used to differ.
    assert app_module._dir_digest(theirs) != app_module._dir_digest(ours)


@pytest.mark.asyncio
async def test_it_names_the_hook_files_it_owns(mu):
    """Comparing a subset is only reproducible if the client is told WHICH files,
    so the answer travels with the digest rather than being guessed."""
    out = await app_module.kb_setup_machine()
    names = out["hooks"]["files"]
    assert "engram_presence_hook.py" in names
    assert all(n.startswith("engram") for n in names)
    assert "README.md" not in names
    assert "hooks.files" in out["local_digest_recipe"]


@pytest.mark.asyncio
async def test_a_changed_hook_still_shows_as_stale(mu, tmp_path):
    """The subset must not be so narrow that a real update stops registering."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "engram_statusline.py").write_text("old", encoding="utf-8")
    b = tmp_path / "b"
    b.mkdir()
    (b / "engram_statusline.py").write_text("new", encoding="utf-8")
    assert app_module._dir_digest(a, app_module._owned_hooks(a)) != \
        app_module._dir_digest(b, app_module._owned_hooks(b))


@pytest.mark.asyncio
async def test_the_status_line_gets_its_own_credentials(mu):
    """The server's own machine skips upload.json by design — which used to leave
    the status line blind on the machine Hiren works on most. Reading state and
    writing presence are different decisions; they get different files."""
    out = await app_module.kb_setup_machine()
    sl = out["status_line"]
    assert "statusline.json" in sl["needs_credentials"]
    assert "even on the server's own machine" in sl["needs_credentials"]
    assert set(sl["merge"]["contents"]) >= {"parent", "url", "token"}


@pytest.mark.asyncio
async def test_the_machine_token_actually_resolves_to_the_user(mu, monkeypatch):
    """THE BUG THIS PINS. Setup synthesized the token's subject from the email
    ("google:<email>") while the account had signed in with GitHub, so every token
    it ever handed out 401'd on its first request. Nothing surfaced it: the
    presence hook drops failed uploads by design, so the machine just never
    appeared in the roster — the exact symptom setup exists to cure. Asserting the
    token is non-empty was never enough; it has to round-trip."""
    out = await app_module.kb_setup_machine()
    token = out["token"]
    assert token

    dash = app_module._dashboard
    claims = dash.auth.verify(token, expected_scope="notify")
    assert claims is not None, "token failed its own scope check"
    user = mu.tenancy.user_by_subject(claims.get("sub", ""))
    assert user is not None, "token subject resolves to nobody — every call 401s"
    assert user.handle == "alice"


@pytest.mark.asyncio
async def test_the_token_is_scope_limited(mu):
    """It rides in a config file on every machine, so it must not be able to act
    as a session — strictly weaker than the connection that minted it."""
    out = await app_module.kb_setup_machine()
    dash = app_module._dashboard
    assert dash.auth.verify(out["token"], expected_scope="notify") is not None
    assert dash.auth.verify(out["token"], expected_scope="session") is None
