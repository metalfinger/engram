"""engram-doctor tests — the round-trip self-test passes on a healthy brain and,
critically, leaves NO commit behind (git HEAD unchanged)."""

from __future__ import annotations

import pytest

from engram_server.config import Settings
from engram_server.doctor import run_doctor
from engram_server.kbstore import KBStore


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


def _by_name(report: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in report["checks"]}


async def test_doctor_passes_core_checks_and_leaves_no_commit(store: KBStore) -> None:
    head_before = store.repo.head_sha().strip()

    report = await run_doctor(store.settings, store)

    status = _by_name(report)
    # Core plumbing all green on a healthy test brain.
    assert status["git"] == "pass"
    assert status["projects"] == "pass"
    assert status["okf-roundtrip"] == "pass"
    assert status["semantic"] == "pass"  # text-only (no qdrant configured) is expected-pass
    assert status["oauth-store"] == "pass"
    assert status["scheduler"] == "pass"
    assert status["no-commit"] == "pass"
    assert report["status"] == "pass"

    # Counts present and sane.
    assert report["counts"]["concepts"] > 0
    assert "artifacts" in report["counts"]
    assert "orphans" in report["counts"]

    # The doctor is non-mutating: HEAD is exactly where it started.
    assert store.repo.head_sha().strip() == head_before


async def test_doctor_builds_own_store_when_none_passed(store: KBStore) -> None:
    # Passing settings alone (no store) still runs — doctor constructs its own KBStore.
    report = await run_doctor(store.settings)
    assert report["status"] in ("pass", "warn")
    assert any(c["name"] == "git" for c in report["checks"])
