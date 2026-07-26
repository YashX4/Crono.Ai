"""Manual live test: classifies today's remaining events, then proposes a day layout
filling the FLEXIBLE_BUCKET ones with open Reminders.

Run with: .venv/bin/python scripts/test_day_layout.py
Must run from Terminal.app (not VS Code) for Calendar/Reminders access to work.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.bucket_context import load_bucket_context
from timeblock_agent.classify import classify_events
from timeblock_agent.config import load_rules
from timeblock_agent.day_layout import plan_day
from timeblock_agent.eventkit_bridge import EventKitBridge
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
    end = now.replace(hour=23, minute=59, second=59)

    events = bridge.list_events(now, end)
    events = [e for e in events if in_calendar_scope(e.calendar_title, rules)]

    classifications = classify_events(events, rules, load_bucket_context())
    bucket_blocks = [e for e in events if classifications[e.identifier] == "FLEXIBLE_BUCKET"]
    fixed_events = [e for e in events if classifications[e.identifier] == "FIXED"]
    specific_events = [e for e in events if classifications[e.identifier] == "FLEXIBLE_SPECIFIC"]
    print(f"{len(bucket_blocks)} FLEXIBLE_BUCKET block(s) to fill:")
    for e in bucket_blocks:
        print(f"  {e.start:%H:%M}-{e.end:%H:%M} {e.title!r} ({e.calendar_title})")

    reminders = bridge.list_reminders(include_completed=False)
    reminders = [r for r in reminders if in_reminder_list_scope(r.list_title, rules)]
    print(f"\n{len(reminders)} open reminder(s) in scope:")
    for r in reminders:
        print(f"  {r.title!r} (priority={r.priority}, due={r.due_date})")

    result = plan_day(
        now, bucket_blocks, reminders, rules, fixed_events=fixed_events,
        bucket_context=load_bucket_context(), specific_events=specific_events,
    )

    print(f"\n{len(result.blocks)} proposed block(s):")
    for b in result.blocks:
        print(f"  [{b.bucket_event_id}] {b.start:%H:%M}-{b.end:%H:%M} {b.title!r} (source={b.source}:{b.source_id})")

    print(f"\n{len(result.unscheduled_reminder_ids)} unscheduled reminder(s): {result.unscheduled_reminder_ids}")

    print(f"\n{len(result.bucket_adjustments)} bucket adjustment(s) (extended for real reminder overflow):")
    for adj in result.bucket_adjustments:
        print(f"  [{adj.bucket_event_id}] -> {adj.new_start:%H:%M}-{adj.new_end:%H:%M}")

    print(f"\nbucket_roles (goal_time_eligible): {result.bucket_roles}")


if __name__ == "__main__":
    main()
