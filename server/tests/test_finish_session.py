"""kb_finish_session — gather the proof, log it, close, release.

The load-bearing case is the RESEARCH chunk: zero commits, and its concepts are
the proof of work. Reporting that as "nothing shipped" would be false, and it is
the case a PR-shaped design would get wrong.
"""

import subprocess
from types import SimpleNamespace

import pytest

import engram_server.app as app_module
from engram_server.registry import StoreRegistry

_DOC = "---\ntype: note\ndescription: d\n---\n\n# T\n\nBody.\n"


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
    monkeypatch.setattr(app_module, "_push_notification", lambda *a, **k: None)
    inv = registry.tenancy.create_invite("alice@example.com")
    registry.tenancy.accept_invite(inv.token, "alice", "alice@example.com",
                                   "google", "google:alice@example.com")
    monkeypatch.setattr(app_module, "get_access_token",
                        lambda: SimpleNamespace(subject="google:alice@example.com"))
    monkeypatch.setattr(app_module, "_speaker_key", lambda: "sess-a")
    return registry


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "workrepo"
    r.mkdir()
    def git(*a):
        subprocess.run(["git", "-C", str(r), *a], check=True,
                       capture_output=True, encoding="utf-8")
    git("init", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (r / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "-A"); git("commit", "-m", "seed")
    return r


@pytest.mark.asyncio
async def test_a_research_chunk_reports_its_concepts_not_nothing(mu, repo):
    """Zero commits is not zero work. This is the case a PR-shaped design gets
    wrong, and the reason finish detects rather than being told."""
    await app_module.kb_prepare_session("alt", "Research the options", str(repo))
    store = await app_module.current_store()
    await store.kb_write("projects/alt/research/findings.md", _DOC, "findings")

    out = await app_module.kb_finish_session(
        "research-the-options", "alt", str(repo), summary="Explored three options.",
    )
    assert out["commits"] == []
    assert any("findings.md" in c for c in out["concepts"])
    assert out["logged"] is True
    log = await store.kb_read("projects/alt/log.md")
    assert "the output of this chunk is the concepts" in log["content"]


@pytest.mark.asyncio
async def test_a_code_chunk_reports_its_commits(mu, repo):
    out = await app_module.kb_prepare_session("alt", "Write the parser", str(repo))
    wt = out["worktree"]
    (SimpleNamespace(),)
    subprocess.run(["git", "-C", wt, "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.name", "t"], check=True)
    with open(f"{wt}/parser.py", "w", encoding="utf-8") as fh:
        fh.write("# parser\n")
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-m", "feat: parser"], check=True,
                   capture_output=True)

    res = await app_module.kb_finish_session(
        "write-the-parser", "alt", str(repo), summary="Parser done.",
    )
    assert [c["subject"] for c in res["commits"]] == ["feat: parser"]


@pytest.mark.asyncio
async def test_claims_are_released(mu, repo):
    await app_module.kb_prepare_session(
        "alt", "Touch theme", str(repo), files=["app/ui/theme.ts"],
    )
    res = await app_module.kb_finish_session("touch-theme", "alt", str(repo))
    assert res["claims_released"] == 1
    store = await app_module.current_store()
    live = [c for c in await store.kb_claims() if not c.get("stale")]
    assert not any(c["path"] == "app/ui/theme.ts" for c in live)


@pytest.mark.asyncio
async def test_claims_are_released_even_when_the_log_fails(mu, repo, monkeypatch):
    """A stale claim outlives the session and misinforms everyone, so releasing
    must not sit on the success path."""
    await app_module.kb_prepare_session(
        "alt", "Fragile chunk", str(repo), files=["app/ui/x.ts"],
    )
    store = await app_module.current_store()

    async def _boom(*a, **k):
        raise RuntimeError("log unavailable")

    monkeypatch.setattr(store, "kb_append_log", _boom)
    res = await app_module.kb_finish_session("fragile-chunk", "alt", str(repo))
    assert res["logged"] is False
    assert res["claims_released"] == 1


@pytest.mark.asyncio
async def test_a_pr_is_never_opened_only_proposed(mu, repo):
    """Two PRs opened without asking had to be closed. The thread blocks on the
    person and stays OPEN until they answer."""
    await app_module.kb_prepare_session("alt", "Ship it", str(repo))
    res = await app_module.kb_finish_session(
        "ship-it", "alt", str(repo),
        pr_title="feat: ship it", pr_body="Does the thing.",
    )
    assert res["awaiting_human"] == "feat: ship it"
    assert res["thread_closed"] is False
    assert any("waiting on the user" in w for w in res["warnings"])
    read = await app_module.kb_thread_read("ship-it", sender="windows")
    assert "Open this PR?" in read["floor"]["awaiting_human"]


@pytest.mark.asyncio
async def test_without_a_pr_the_thread_closes(mu, repo):
    await app_module.kb_prepare_session("alt", "Quiet chunk", str(repo))
    res = await app_module.kb_finish_session("quiet-chunk", "alt", str(repo))
    assert res["thread_closed"] is True
    read = await app_module.kb_thread_read("quiet-chunk", sender="windows")
    assert read["status"] == "closed"
