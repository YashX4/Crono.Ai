"""pmset wake scheduling + caffeinate wrapping (see spec: "Sleep/wake handling").

The persistent scheduler computes its own next trigger time each run and schedules the
Mac to wake for exactly that moment, rather than a flat cadence. Requires passwordless
sudo for `pmset schedule` specifically — see the manual setup instructions; `sudo -n` is
used throughout so a missing sudoers entry fails fast (and gets logged) instead of
hanging the whole process waiting on a password prompt that can never arrive in a
background service.

caffeinate wraps each active tick (not the whole long-running process — the machine
should still be able to sleep between triggers, that's the whole point of pmset waking it
back up) so a real EventKit/Claude call in progress doesn't get interrupted by the Mac
falling asleep mid-write.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import Optional

logger = logging.getLogger("timeblock_agent.wake_schedule")

_last_scheduled_wake: Optional[datetime] = None


def _pmset_time_format(at: datetime) -> str:
    return at.strftime("%m/%d/%y %H:%M:%S")


def schedule_wake(at: datetime) -> None:
    """Cancels the previously scheduled wake (if any) and schedules a new one for `at`.
    pmset schedule accumulates entries rather than replacing them, so the previous one
    must be explicitly cancelled or stale wake times pile up indefinitely."""
    global _last_scheduled_wake

    if _last_scheduled_wake is not None:
        try:
            subprocess.run(
                ["sudo", "-n", "pmset", "schedule", "cancel", "wake", _pmset_time_format(_last_scheduled_wake)],
                capture_output=True, timeout=10,
            )
        except Exception:
            logger.exception("Failed to cancel previous pmset wake schedule")

    try:
        result = subprocess.run(
            ["sudo", "-n", "pmset", "schedule", "wake", _pmset_time_format(at)],
            capture_output=True, timeout=10, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "pmset schedule wake failed (rc=%d): %s — is passwordless sudo set up for this command?",
                result.returncode, result.stderr.strip(),
            )
            _last_scheduled_wake = None
            return
        _last_scheduled_wake = at
    except Exception:
        logger.exception("Failed to schedule pmset wake for %s", at)
        _last_scheduled_wake = None


def caffeinate_briefly(seconds: int = 90) -> None:
    """Fire-and-forget self-expiring assertion so the Mac won't idle-sleep mid-tick.
    Deliberately NOT wrapping the whole persistent process — it should still be able to
    sleep between triggers, since pmset is what wakes it back up at the right time."""
    try:
        subprocess.Popen(["caffeinate", "-i", "-t", str(seconds)])
    except Exception:
        logger.exception("Failed to spawn caffeinate")
