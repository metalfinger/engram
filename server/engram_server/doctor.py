"""engram-doctor — a non-mutating round-trip self-test of a brain deployment.

Where the nightly reconcile audits the CONTENT of the bundle, the doctor audits the
PLUMBING: is the checkout there, does git answer, do reads work, is the OKF
write/read/serialize pipeline sound, is the vector backend reachable (or cleanly
text-only), is the OAuth store loadable, is the scheduler config sane. It never leaves
a commit behind — the write/read/delete round-trip runs in a throwaway temp dir, and
the real brain's git HEAD is captured before and after and asserted unchanged.

Exposed two ways: ``kb_doctor()`` (an MCP tool, so a session can ask "is my brain
healthy?") and ``python -m engram_server.doctor`` (a CLI / CI check with a readable
report and a non-zero exit on any failed check).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from anyio import to_thread

from .config import Settings, get_settings
from .frontmatter import read_meta, split, validate_concept
from .kbstore import KBStore, _read_text_retry, _write_text
from .reconcile import _scan
from .scheduler import _parse_hhmm

log = logging.getLogger("engram.doctor")


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    """One report row: status is 'pass' | 'warn' | 'fail'."""
    return {"name": name, "status": status, "detail": detail}


async def _roundtrip_check() -> dict[str, str]:
    """Exercise the OKF write->read->delete pipeline in a throwaway temp dir (never the
    brain), using the store's own file helpers + frontmatter validation."""
    def _work() -> str:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "doctor-roundtrip.md"
            text = (
                "---\ntype: doctor\ntitle: Doctor Round Trip\n"
                "description: engram-doctor self-test concept.\n---\n\nround-trip body\n"
            )
            validated, meta, _warnings = validate_concept(text, rel_path="doctor-roundtrip.md")
            _write_text(path, validated)
            back = _read_text_retry(path)
            if back != validated:
                raise AssertionError("read-back content differs from what was written")
            doc = split(back)
            if doc is None or read_meta(path).get("type") != "doctor":
                raise AssertionError("frontmatter did not round-trip")
            path.unlink()
            if path.exists():
                raise AssertionError("delete left the file behind")
        return "write -> read -> delete round-trip clean (throwaway temp dir)"

    try:
        detail = await to_thread.run_sync(_work)
        return _check("okf-roundtrip", "pass", detail)
    except Exception as exc:  # noqa: BLE001
        return _check("okf-roundtrip", "fail", f"OKF pipeline round-trip failed: {exc}")


async def run_doctor(settings: Settings, store: KBStore | None = None) -> dict[str, Any]:
    """Run every plumbing check and return a structured report::

        {status, checks: [{name, status, detail}], counts, head}

    ``status`` is the worst of the check statuses ('fail' > 'warn' > 'pass').
    Non-mutating: leaves no commit behind (git HEAD is asserted unchanged)."""
    store = store or KBStore(settings)
    checks: list[dict[str, str]] = []

    # (a) brain checkout exists + git answers.
    head_before: str | None = None
    if not (settings.brain_path / ".git").exists():
        checks.append(
            _check("git", "fail", f"no brain checkout at {settings.brain_path} — clone it first")
        )
    else:
        try:
            head_before = (await to_thread.run_sync(store.repo.head_sha)).strip()
            checks.append(_check("git", "pass", f"checkout OK, HEAD {head_before[:12]}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("git", "fail", f"git rev-parse failed: {exc}"))

    # (b) reads work — projects resolve.
    projects: list[dict[str, Any]] = []
    try:
        projects = await store.kb_projects()
        if projects:
            checks.append(_check("projects", "pass", f"{len(projects)} project(s) readable"))
        else:
            checks.append(_check("projects", "fail", "kb_projects returned none"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("projects", "fail", f"kb_projects failed: {exc}"))

    # (c) OKF write->read->delete round-trip (throwaway temp dir; no commit).
    checks.append(await _roundtrip_check())

    # (d) semantic backend reachable, or cleanly text-only.
    if store.semantic is None:
        checks.append(
            _check("semantic", "pass", "text-only (no qdrant_url configured) — expected")
        )
    else:
        ok = await to_thread.run_sync(store.semantic.ensure_collection)
        if ok:
            checks.append(_check("semantic", "pass", "qdrant collection reachable"))
        else:
            checks.append(
                _check("semantic", "warn", "qdrant configured but unreachable — search is text-only")
            )

    # (e) OAuth store loadable (when a persistence path is configured).
    store_path = settings.oauth_store_path
    if not store_path:
        checks.append(_check("oauth-store", "pass", "in-memory (no persistence path set)"))
    elif not Path(store_path).exists():
        checks.append(
            _check("oauth-store", "warn", f"path set but no file yet at {store_path} (first login creates it)")
        )
    else:
        try:
            from .oauth.store import InMemoryOAuthStore

            await to_thread.run_sync(lambda: InMemoryOAuthStore(path=store_path))
            checks.append(_check("oauth-store", "pass", f"loadable ({store_path})"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("oauth-store", "fail", f"oauth store unreadable: {exc}"))

    # (f) scheduler config sanity (HH:MM times parse).
    if not settings.scheduler_enabled:
        checks.append(_check("scheduler", "pass", "disabled by config"))
    else:
        # v3: an EMPTY time means that job is deliberately off (briefing_at defaults
        # to '' — the push briefing is retired). Only a non-empty unparseable value
        # is a misconfiguration.
        bad = [
            f"{label}={value!r}"
            for label, value in (("reconcile_at", settings.reconcile_at), ("briefing_at", settings.briefing_at))
            if value and _parse_hhmm(value) is None
        ]
        if bad:
            checks.append(_check("scheduler", "warn", f"unparseable time(s): {', '.join(bad)}"))
        else:
            briefing = settings.briefing_at or "off (pull-only)"
            checks.append(
                _check(
                    "scheduler",
                    "pass",
                    f"reconcile={settings.reconcile_at} briefing={briefing}",
                )
            )

    # (g) counts — a quick read-only scan of the bundle.
    counts: dict[str, int] = {}
    try:
        scan = await to_thread.run_sync(lambda: _scan(store.root))
        artifacts = await to_thread.run_sync(lambda: store._artifacts_sync(None))
        unread = sum(int(p.get("unread_messages") or 0) for p in projects)
        counts = {
            "concepts": scan["total_files"],
            "artifacts": len(artifacts),
            "unread_messages": unread,
            "orphans": len(scan["orphans"]),
        }
        checks.append(
            _check(
                "counts",
                "pass",
                f"{counts['concepts']} concepts, {counts['artifacts']} artifacts, "
                f"{counts['unread_messages']} unread, {counts['orphans']} orphans",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("counts", "warn", f"scan failed: {exc}"))

    # Non-mutating guarantee: HEAD must be exactly where it was.
    if head_before is not None:
        try:
            head_after = (await to_thread.run_sync(store.repo.head_sha)).strip()
            if head_after != head_before:
                checks.append(
                    _check("no-commit", "fail", f"git HEAD moved during doctor: {head_before[:12]} -> {head_after[:12]}")
                )
            else:
                checks.append(_check("no-commit", "pass", "git HEAD unchanged (non-mutating)"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("no-commit", "warn", f"could not re-read HEAD: {exc}"))

    order = {"fail": 2, "warn": 1, "pass": 0}
    status = max((c["status"] for c in checks), key=lambda s: order.get(s, 0), default="pass")
    return {"status": status, "checks": checks, "counts": counts, "head": head_before}


def _format_report(report: dict[str, Any]) -> str:
    """Human-readable rendering of a run_doctor report."""
    icon = {"pass": "OK ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"engram-doctor: {report['status'].upper()}", ""]
    for c in report["checks"]:
        lines.append(f"  [{icon.get(c['status'], '?')}] {c['name']}: {c['detail']}")
    if report.get("counts"):
        lines.append("")
        lines.append("  counts: " + ", ".join(f"{k}={v}" for k, v in report["counts"].items()))
    return "\n".join(lines)


def main() -> int:
    import anyio

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    report = anyio.run(run_doctor, settings)
    print(_format_report(report))
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
