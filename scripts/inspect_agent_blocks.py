"""Dumps full details (including the embedded agent-meta marker) of today's
agent-authored blocks, for debugging what a replan actually did.

Run with: .venv/bin/python scripts/inspect_agent_blocks.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.config import load_rules
from timeblock_agent.diff_write import decode_block_meta
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH


def main():
    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()
    cal_granted, _ = bridge.request_access()
    if not cal_granted:
        print("Calendar access not granted.")
        return

    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0)
    day_end = now.replace(hour=23, minute=59, second=59)

    events = bridge.list_events(day_start, day_end, calendar_titles=[rules.agent_calendar])
    all_events = bridge.list_events(day_start, day_end)  # every calendar, to look up bucket containers
    all_by_id = {e.identifier: e for e in all_events}
    reminders_by_id = {r.identifier: r for r in bridge.list_reminders(include_completed=True)}

    for e in events:
        meta = decode_block_meta(e.notes)
        print(f"{e.start:%H:%M}-{e.end:%H:%M} {e.title!r}")
        print(f"  identifier: {e.identifier}")
        print(f"  decoded meta: {meta}")
        if meta:
            bucket = all_by_id.get(meta.get("bucket_event_id"))
            if bucket:
                slack_before = (e.start - bucket.start).total_seconds() / 60
                slack_after = (bucket.end - e.end).total_seconds() / 60
                print(
                    f"  parent bucket: {bucket.title!r} {bucket.start:%H:%M}-{bucket.end:%H:%M} "
                    f"(slack before task: {slack_before:.0f}min, slack after task: {slack_after:.0f}min)"
                )
            else:
                print("  parent bucket: NOT FOUND")
        if meta and meta.get("source") == "reminder":
            reminder = reminders_by_id.get(meta.get("source_id"))
            if reminder:
                match = "MATCHES" if reminder.title == e.title else "*** MISMATCH ***"
                print(f"  actual reminder title: {reminder.title!r} ({match})")
            else:
                print("  actual reminder: NOT FOUND (deleted, completed-and-excluded, or bad id)")
        print()


if __name__ == "__main__":
    main()
