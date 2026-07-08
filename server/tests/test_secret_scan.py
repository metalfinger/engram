"""Secret scan at the share boundary: kb_share_artifact refuses to publish likely
secrets, names the KINDS not the values, and can be deliberately overridden."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.errors import KBError
from engram_server.frontmatter import read_meta
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _artifact(body: str) -> str:
    return (
        "---\ntype: artifact\ndescription: A doc.\nsources:\n  - projects/alt/context.md\n---\n\n"
        f"{body}\n"
    )


SECRETS = {
    "OpenAI API key": "key = sk-abcdefghijklmnopqrstuvwxyz0123456789",
    "GitHub token": "creds ghp_" + "a" * 40,
    "AWS access key id": "AKIAIOSFODNN7EXAMPLE",
    "bearer token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
    "private key block": "-----BEGIN RSA PRIVATE KEY-----",
    "hardcoded credential": 'password = "hunter2secret"',
}


@pytest.mark.parametrize(("kind", "line"), list(SECRETS.items()))
async def test_share_blocks_each_secret_kind(store: KBStore, kind: str, line: str) -> None:
    path = "projects/alt/artifacts/2026-07-leaky.md"
    await store.kb_write(path, _artifact(f"Intro paragraph.\n{line}"), "save leaky")
    with pytest.raises(KBError) as exc:
        await store.kb_share_artifact(path)
    msg = str(exc.value)
    assert kind in msg
    # the actual secret value is never echoed back
    assert line.split()[-1] not in msg
    # and nothing was written — no share token minted
    assert "share" not in read_meta(store.root / path)


async def test_share_reports_line_numbers_not_values(store: KBStore) -> None:
    path = "projects/alt/artifacts/2026-07-leaky2.md"
    await store.kb_write(path, _artifact("Line one.\nLine two.\nAKIAIOSFODNN7EXAMPLE"), "save")
    with pytest.raises(KBError, match="line") as exc:
        await store.kb_share_artifact(path)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(exc.value)


async def test_allow_secrets_overrides(store: KBStore, settings: Settings) -> None:
    path = "projects/alt/artifacts/2026-07-leaky3.md"
    await store.kb_write(path, _artifact("AKIAIOSFODNN7EXAMPLE"), "save")
    r = await store.kb_share_artifact(path, allow_secrets=True)
    assert "/share/" in r["share_url"]
    assert read_meta(settings.brain_path / path).get("share")


async def test_clean_artifact_shares_fine(store: KBStore) -> None:
    path = "projects/alt/artifacts/2026-07-clean.md"
    await store.kb_write(path, _artifact("Nothing sensitive here, just ordinary prose."), "save")
    r = await store.kb_share_artifact(path)
    assert "/share/" in r["share_url"]


async def test_already_shared_skips_scan(store: KBStore) -> None:
    # share a clean artifact, then append a secret; re-sharing is idempotent and must not
    # re-scan (the link already exists — scanning only guards the moment of minting).
    path = "projects/alt/artifacts/2026-07-grandfathered.md"
    await store.kb_write(path, _artifact("Clean at first."), "save")
    r1 = await store.kb_share_artifact(path)
    await store.kb_edit(path, "append", "AKIAIOSFODNN7EXAMPLE")
    r2 = await store.kb_share_artifact(path)
    assert r2["share_url"] == r1["share_url"]
