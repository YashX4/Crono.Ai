"""Manual live test: simulates a "running behind" check-in answer on whatever
FLEXIBLE_BUCKET block is active right now, and shows how the incremental replan
cascades later blocks. Does NOT write to the calendar — read-only dry run.

Run with: .venv/bin/python scripts/test_incremental_replan.py
Must run from Terminal.app (not VS Code) for Calendar/Reminders access.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.bucket_context import load_bucket_context
from timeblock_agent.classify import classify_events
from timeblock_agent.config import load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.incremental_replan import replan_incremental
from timeblock_agent.orchestrator import RULES_PATH
from timeblock_agent.scope import in_calendar_scope, in_reminder_list_scope


def main():
    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()

    cal_granted, rem_granted = bridge.request_access()
    if not (cal_granted and rem_granted):
        print("Calendar/Reminders access not granted.")
        return

    now = datetime.now()
    day_end = now.replace(hour=23, minute=59, second=59)

    events = bridge.list_events(now.replace(hour=0, minute=0, second=0), day_end)
    events = [e for e in events if in_calendar_scope(e.calendar_title, rules)]

    classifications = classify_events(events, rules, load_bucket_context())
    bucket_blocks = [e for e in events if classifications[e.identifier] == "FLEXIBLE_BUCKET"]
    fixed_events = [e for e in events if classifications[e.identifier] == "FIXED"]
    specific_events = [e for e in events if classifications[e.identifier] == "FLEXIBLE_SPECIFIC"]

    active = [e for e in bucket_blocks if e.start <= now <= e.end]
    if not active:
        upcoming = [e for e in bucket_blocks if e.start > now]
        if not upcoming:
            print("No active or upcoming FLEXIBLE_BUCKET block found to simulate a check-in on.")
            return
        triggering = min(upcoming, key=lambda e: e.start)
        print(f"Nothing active right now; simulating on the next upcoming block instead: {triggering.title!r}")
    else:
        triggering = active[0]

    print(f"Simulating 'running_behind' on: {triggering.start:%H:%M}-{triggering.end:%H:%M} {triggering.title!r}")

    reminders = bridge.list_reminders(include_completed=False)
    reminders = [r for r in reminders if in_reminder_list_scope(r.list_title, rules)]

    remaining_buckets = [e for e in bucket_blocks if e.end > now]
    result = replan_incremental(
        now, "running_behind", triggering.identifier, remaining_buckets, reminders, rules,
        fixed_events=fixed_events, bucket_context=load_bucket_context(), specific_events=specific_events,
    )

    print(f"\n{len(result.blocks)} adjusted block(s):")
    for b in result.blocks:
        print(f"  [{b.bucket_event_id}] {b.start:%H:%M}-{b.end:%H:%M} {b.title!r} (source={b.source})")
    print(f"{len(result.unscheduled_reminder_ids)} unscheduled reminder(s)")


if __name__ == "__main__":
    main()
