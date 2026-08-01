"""Per-user notification wake bus (v3.1) — the live half of desktop delivery.

The tray app long-polls /dashboard/api/notifications?wait=N: the request PARKS
here until something new lands for that user (or the timeout lapses), so a
room invite reaches the desktop in ~1s instead of "whenever the next poll is",
and an idle tray costs one parked request instead of a request a minute.

Same primitive as the rooms bus (teamwork.room_notify/room_wait), same rules:
in-process only (one server, one loop), asyncio.Condition per user, and every
in-process notification writer calls wake(). Writers OUTSIDE the process (an
operator script inserting rows directly) can't wake it — those arrive at the
end of the current wait cycle, which the timeout guarantees.
"""

from __future__ import annotations

import asyncio

_conditions: dict[int, asyncio.Condition] = {}


def _cond(user_id: int) -> asyncio.Condition:
    cond = _conditions.get(user_id)
    if cond is None:
        cond = _conditions[user_id] = asyncio.Condition()
    return cond


async def wake(user_id: int) -> None:
    """Wake every request parked on this user — call after ANY notification write."""
    cond = _cond(user_id)
    async with cond:
        cond.notify_all()


async def wait(user_id: int, seconds: float) -> None:
    """Park until wake(user_id) or the timeout lapses (clamped 1..55s — under
    every proxy/idle timeout in the chain)."""
    cond = _cond(user_id)
    try:
        async with cond:
            await asyncio.wait_for(cond.wait(), timeout=max(1.0, min(55.0, seconds)))
    except (TimeoutError, asyncio.TimeoutError):
        pass
