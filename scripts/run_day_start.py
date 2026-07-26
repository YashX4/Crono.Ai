"""The actual Day-Start Full Plan pipeline (see spec): classifies today's remaining
events, proposes a layout for the FLEXIBLE_BUCKET ones from open Reminders (+ a goal-fill
candidate for any protected buffer / idle bucket, and a "Goal Time" bucket sourced from
Work if you pass minutes), and writes only the deltas into the dedicated agent calendar.

Delegates to orchestrator.run_day_start_pass — the same function the real server calls —
rather than re-deriving that (now fairly involved) sequencing here, so this script can
never drift out of sync with what actually runs.

This WRITES REAL EVENTS to your Calendar (in rules.yaml's `agent_calendar` — or
rules.test.yaml's, if TIMEBLOCK_RULES_PATH is set; see TESTING.md). Safe to re-run —
reruns diff against what's already there and only changes what's actually different. Must
run from Terminal.app (not VS Code) for Calendar/Reminders access.

Usage: .venv/bin/python scripts/run_day_start.py [goal_time_minutes]
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent import orchestrator
from timeblock_agent.bucket_context import load_bucket_context
from timeblock_agent.classify import classify_events
from timeblock_agent.config import load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH
from timeblock_agent.scope import in_calendar_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def _confirm_real_data_run() -> bool:
    """This script WRITES real calendar events. If none of the TIMEBLOCK_*_PATH sandbox
    env vars are set (a new Terminal tab forgetting `source scripts/sandbox_env.sh` —
    already happened twice live, the second time creating and then deleting 5 real task
    blocks mixed in with real reminders), silently falling back to real rules.yaml/real
    calendars is exactly the wrong default for a script this is this easy to misfire.
    Require an explicit typed confirmation instead of a flag that's easy to forget."""
    if os.environ.get("TIMEBLOCK_RULES_PATH"):
        return True
    print(
        "\n*** WARNING: no TIMEBLOCK_RULES_PATH set — this will run against your REAL "
        "rules.yaml, REAL calendars, and REAL reminders, and WILL WRITE real calendar "
        "events. ***\n"
        "If you meant to test in the sandbox, Ctrl+C now and run:\n"
        "  source scripts/sandbox_env.sh\n"
    )
    answer = input("Type 'yes' to proceed against REAL data, anything else to abort: ")
    return answer.strip().lower() == "yes"


def main():
    if not _confirm_real_data_run():
        print("Aborted.")
        return

    goal_time_minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()

    cal_granted, rem_granted = bridge.request_access()
    if not (cal_granted and rem_granted):
        print("Calendar/Reminders access not granted.")
        return

    now = datetime.now()
    day_end = now.replace(hour=23, minute=59, second=59)

    events = bridge.list_events(now, day_end)
    events = [e for e in events if in_calendar_scope(e.calendar_title, rules)]

    classifications = classify_events(events, rules, load_bucket_context())
    bucket_blocks = [e for e in events if classifications[e.identifier] == "FLEXIBLE_BUCKET"]
    fixed_events = [e for e in events if classifications[e.identifier] == "FIXED"]
    specific_events = [e for e in events if classifications[e.identifier] == "FLEXIBLE_SPECIFIC"]
    print(f"{len(bucket_blocks)} FLEXIBLE_BUCKET block(s), {len(fixed_events)} FIXED event(s), "
          f"{len(specific_events)} FLEXIBLE_SPECIFIC event(s), goal_time_minutes={goal_time_minutes}")
    for e in bucket_blocks:
        print(f"  {e.start:%H:%M}-{e.end:%H:%M} {e.title!r} ({e.calendar_title})")

    at_risk_titles, actual_goal_minutes = orchestrator.run_day_start_pass(
        bridge, rules, now, bucket_blocks, fixed_events, goal_time_minutes, specific_events
    )
    print("\nDone — see log lines above for what actually got created/updated/deleted.")
    if at_risk_titles:
        print(f"Behind pace for their deadline: {', '.join(at_risk_titles)}")
    if goal_time_minutes > 0 and actual_goal_minutes < goal_time_minutes:
        print(f"Goal time shortfall: only sourced {actual_goal_minutes} of {goal_time_minutes} requested min.")


if __name__ == "__main__":
    main()
