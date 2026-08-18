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
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "google", "google:alice@example.com")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="google:alice@example.com"))
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
    """A status line is personal — his may already carry branch, PR state or CI.
    Replacing it silently would be the rudest thing setup could do."""
    out = await app_module.kb_setup_machine()
    assert "DO NOT replace" in out["status_line"]["if_one_exists"]
    assert "OPTIONAL" in " ".join(out["steps"])
