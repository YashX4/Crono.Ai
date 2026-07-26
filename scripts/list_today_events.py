"""Prints every in-scope event for the rest of today with its classification and
EventKit identifier — unlike list_agent_blocks.py (agent-authored task blocks only), this
covers FIXED and FLEXIBLE_SPECIFIC events too (e.g. Test Meeting, Test Gym), useful for
forcing a check-in on one via send_real_checkin.py.

Run with: .venv/bin/python scripts/list_today_events.py
Must run from Terminal.app (not VS Code) for Calendar access.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.bucket_context import load_bucket_context
from timeblock_agent.classify import classify_events
from timeblock_agent.config import load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH
from timeblock_agent.scope import in_calendar_scope


def main():
    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()
    cal_granted, _ = bridge.request_access()
    if not cal_granted:
        print("Calendar access not granted.")
        return

    now = datetime.now()
    end = now.replace(hour=23, minute=59, second=59)
    events = bridge.list_events(now, end)
    events = [e for e in events if in_calendar_scope(e.calendar_title, rules)]

    if not events:
        print("(nothing left today)")
        return

    result = classify_events(events, rules, load_bucket_context())
    for e in sorted(events, key=lambda e: e.start):
        print(f"{e.start:%H:%M}-{e.end:%H:%M} {e.title!r} ({e.calendar_title}) -> {result[e.identifier]}")
        print(f"  id: {e.identifier}")


if __name__ == "__main__":
    main()
