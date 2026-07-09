"""In-process daily scheduler for the nightly reconcile + morning briefing.

Runs a single daemon thread that wakes periodically, and when a job's local-clock
time (HH:MM) comes due submits the corresponding coroutine to the SERVER's event
loop via ``run_coroutine_threadsafe`` — so all store mutations run on the one loop
that owns the store's asyncio.Lock. The thread never touches the store directly.
Every fire is wrapped so a failing job logs and the server keeps running.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine

from .briefing import build_briefing
from .config import Settings
from .presence_spool import ingest_spool
from .reconcile import run_reconcile

log = logging.getLogger("engram.scheduler")

_POLL_SECONDS = 30.0


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    log.warning("scheduler: invalid time %r — job disabled", value)
    return None


def _next_dt(hh: int, mm: int, after: datetime) -> datetime:
    candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


class Scheduler:
    """Daily HH:MM scheduler bridging a daemon thread to the server event loop."""

    def __init__(self, store: Any, settings: Settings, loop: asyncio.AbstractEventLoop) -> None:
        self._store = store
        self._settings = settings
        self._loop = loop
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="engram-scheduler", daemon=True)
        self._jobs: list[tuple[str, tuple[int, int], Callable[[], Coroutine[Any, Any, Any]]]] = []
        # Interval jobs fire every N seconds (vs the daily HH:MM jobs above).
        self._interval_jobs: list[tuple[str, float, Callable[[], Coroutine[Any, Any, Any]]]] = []
        self._poll = _POLL_SECONDS

    def start(self) -> None:
        reconcile_t = _parse_hhmm(self._settings.reconcile_at)
        briefing_t = _parse_hhmm(self._settings.briefing_at)
        if reconcile_t:
            self._jobs.append(
                ("reconcile", reconcile_t, lambda: run_reconcile(self._store, self._store.semantic))
            )
        if briefing_t:
            self._jobs.append(("briefing", briefing_t, lambda: build_briefing(self._store)))
        ingest_secs = self._settings.presence_ingest_seconds
        if ingest_secs and ingest_secs > 0:
            self._interval_jobs.append(
                ("presence-ingest", float(ingest_secs),
                 lambda: ingest_spool(self._store, self._settings))
            )
            # Poll at least as often as the tightest interval so it can actually fire.
            self._poll = min(self._poll, max(1.0, float(ingest_secs)))
        if not self._jobs and not self._interval_jobs:
            log.warning("scheduler: no valid jobs configured — not starting")
            return
        self._thread.start()
        log.info(
            "scheduler: started (reconcile=%s briefing=%s presence-ingest=%ss)",
            self._settings.reconcile_at,
            self._settings.briefing_at,
            ingest_secs,
        )

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        now = datetime.now()
        next_fire = {name: _next_dt(hh, mm, now) for name, (hh, mm), _ in self._jobs}
        interval_next = {
            name: now + timedelta(seconds=secs) for name, secs, _ in self._interval_jobs
        }
        while not self._stop.is_set():
            now = datetime.now()
            for name, (hh, mm), factory in self._jobs:
                if now >= next_fire[name]:
                    self._fire(name, factory)
                    next_fire[name] = _next_dt(hh, mm, now)
            for name, secs, factory in self._interval_jobs:
                if now >= interval_next[name]:
                    self._fire(name, factory)
                    interval_next[name] = now + timedelta(seconds=secs)
            self._stop.wait(self._poll)

    def _fire(self, name: str, factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        log.info("scheduler: firing %s", name)
        try:
            future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        except Exception:  # noqa: BLE001 — a broken submit must not kill the thread
            log.warning("scheduler: failed to submit %s", name, exc_info=True)
            return

        def _done(f: Any, _name: str = name) -> None:
            try:
                f.result()
            except Exception:  # noqa: BLE001 — job errors are logged, never propagated
                log.warning("scheduler: job %s failed", _name, exc_info=True)

        future.add_done_callback(_done)


def start_schedulers(
    store: Any, settings: Settings, loop: asyncio.AbstractEventLoop
) -> Scheduler | None:
    """Create and start the scheduler unless disabled. Returns it (for stop()) or None."""
    if not settings.scheduler_enabled:
        log.info("scheduler: disabled (ENGRAM_SCHEDULER=0)")
        return None
    sched = Scheduler(store, settings, loop)
    sched.start()
    return sched
