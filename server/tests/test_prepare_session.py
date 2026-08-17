"""kb_prepare_session — one call that sets up a chunk and hands back the command.

Tests are written as the situations that go wrong, because that is what found
every real bug in this subsystem: preparing twice, preparing on a repo that
isn't one, and preparing when the brain is unreachable.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.errors import KBError
from engram_server.registry import StoreRegistry


@pytest.fixture()
def mu(settings, tmp_path, monkeypatch):
    s = settings.model_copy(update={
        "multiuser": True,
        "users_root": str(tmp_path / "users"),
        "tenancy_db_path": str(tmp_path / "engram.db"),
    })
    registry = StoreRegistry(s)
    monkeypatch.setattr(app_module, "registry", registry)
    monkeypatch.setattr(app_module, "settings", s)
    monkeypatch.setattr(app_module, "_presence_last", {})
    monkeypatch.setattr(app_module, "_CLAIMS_CACHE", {"at": 0.0, "rows": []})
    inv = registry.tenancy.create_invite("alice@example.com")
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "google", "google:alice@example.com")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="google:alice@example.com"))
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    return registry


@pytest.fixture()
def repo(tmp_path):
    """A real git repo with one commit on main — worktrees need a base to branch from."""
    r = tmp_path / "workrepo"
    r.mkdir()
    def git(*a):
        subprocess.run(["git", "-C", str(r), *a], check=True,
                       capture_output=True, encoding="utf-8")
    git("init", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (r / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "seed")
    return r


@pytest.mark.asyncio
async def test_prepare_creates_a_worktree_and_returns_a_command(mu, repo):
    out = await app_module.kb_prepare_session(
        "alt", "Reskin the widgets", str(repo),
        goal="Ship the reskin", exit_condition="PR merged or parked",
    )
    assert out["branch"] == "reskin-the-widgets"
    wt = Path(out["worktree"])
    assert wt.is_dir(), "the worktree is the one hard requirement"
    assert str(wt) in out["command"] and "claude" in out["command"]
    assert out["first_message"] == "realign"


@pytest.mark.asyncio
async def test_the_pin_is_written_so_the_new_session_resolves_cold(mu, repo):
    """Without the pin the new session has to be told its project — which is the
    hand-holding this tool exists to remove."""
    out = await app_module.kb_prepare_session("alt", "Fix the parser", str(repo))
    pin = Path(out["worktree"]) / ".engram-project"
    assert pin.read_text(encoding="utf-8").strip() == "alt"


@pytest.mark.asyncio
async def test_the_thread_carries_the_goal_and_exit_condition(mu, repo):
    await app_module.kb_prepare_session(
        "alt", "Fix the parser", str(repo),
        goal="Parser handles nested quotes", exit_condition="Tests green on the branch",
    )
    read = await app_module.kb_thread_read("fix-the-parser", sender="windows")
    assert read["goal"] == "Parser handles nested quotes"
    assert read["exit_condition"] == "Tests green on the branch"


@pytest.mark.asyncio
async def test_claims_make_the_chunk_visible_before_a_keystroke(mu, repo):
    await app_module.kb_prepare_session(
        "alt", "Touch the theme", str(repo), files=["app/ui/theme.ts"],
    )
    monkey = {"at": 0.0, "rows": []}
    app_module._CLAIMS_CACHE.update(monkey)
    claims = await (await app_module.current_store()).kb_claims()
    assert any(c["path"] == "app/ui/theme.ts" for c in claims)


@pytest.mark.asyncio
async def test_the_brief_links_refs_rather_than_pasting_them(mu, repo):
    """A map, not a dump — 'navigate, never ingest' applied to session startup."""
    store = await app_module.current_store()
    await store.kb_write(
        "projects/alt/decisions/tokens.md",
        "---\ntype: decision\ndescription: token naming\n---\n\n# Tokens\n\nUse --ftf-*.\n",
        "seed",
    )
    out = await app_module.kb_prepare_session(
        "alt", "Reskin", str(repo), refs=["projects/alt/decisions/tokens.md"],
    )
    brief = await store.kb_read(out["brief_path"])
    assert "tokens.md" in brief["content"]
    assert "Use --ftf-*" not in brief["content"], "linked, not pasted"


@pytest.mark.asyncio
async def test_preparing_twice_reuses_rather_than_duplicating(mu, repo):
    """The ghost-speaker bug was exactly this shape: a second identity created
    where one already existed."""
    first = await app_module.kb_prepare_session("alt", "Same chunk", str(repo))
    second = await app_module.kb_prepare_session("alt", "Same chunk", str(repo))
    assert first["worktree"] == second["worktree"]
    assert any("already existed" in w for w in second["warnings"])


@pytest.mark.asyncio
async def test_a_path_that_is_not_a_repo_is_refused_clearly(mu, tmp_path):
    with pytest.raises(KBError, match="not a git repository"):
        await app_module.kb_prepare_session("alt", "Nowhere", str(tmp_path))


@pytest.mark.asyncio
async def test_a_missing_repo_path_says_why(mu):
    """The server cannot see the caller's working directory — there is no sensible
    default, so guessing would create worktrees in the wrong place."""
    with pytest.raises(KBError, match="cannot see your working directory"):
        await app_module.kb_prepare_session("alt", "Nowhere", "")


@pytest.mark.asyncio
async def test_an_unnameable_chunk_is_refused(mu, repo):
    with pytest.raises(KBError, match="at least one letter or digit"):
        await app_module.kb_prepare_session("alt", "!!!", str(repo))


@pytest.mark.asyncio
async def test_a_broken_brain_still_returns_a_working_command(mu, repo, monkeypatch):
    """The work is the product. An unprepared session beats no session."""
    store = await app_module.current_store()

    async def _boom(*a, **k):
        raise RuntimeError("brain unreachable")

    monkeypatch.setattr(store, "kb_write", _boom)
    out = await app_module.kb_prepare_session("alt", "Resilient chunk", str(repo))
    assert Path(out["worktree"]).is_dir()
    assert "claude" in out["command"]
    assert any("Brief not written" in w for w in out["warnings"])
