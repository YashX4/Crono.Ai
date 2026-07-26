"""Wipes leftover test events/reminders from PRIOR days only (never today) in the sandbox
test calendars/list — "Test Bucket", "Task Blocks (Test)", "Test Reminders".

setup_test_sandbox.py's own wipe is always scoped to "today" relative to whenever it's
run, so a session that ends without a final cleanup (e.g. stopped mid-testing, as
happened after a late-night session) leaves that day's test events sitting on the
calendar forever — invisible to every later reseed, which only ever looks at ITS OWN
"today". This is the catch-up pass for that: everything strictly before today, in the
same hardcoded test-only calendars/list setup_test_sandbox.py itself uses (safe by
construction — never touches real calendars/reminders regardless of which rules file is
active).

Usage: .venv/bin/python scripts/cleanup_stale_test_events.py [--days N]
  --days N: how many days back to look (default 7)
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.eventkit_bridge import EventKitBridge

TEST_BUCKET_CAL = "Test Bucket"
TEST_AGENT_CAL = "Task Blocks (Test)"
TEST_REMINDER_LIST = "Test Reminders"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="How many days back to look (default 7)")
    args = parser.parse_args()

    bridge = EventKitBridge()
    cal_granted, rem_granted = bridge.request_access()
    if not (cal_granted and rem_granted):
        print("Calendar/Reminders access not granted.")
        return

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today_start - timedelta(days=args.days)

    total = 0
    for cal in (TEST_BUCKET_CAL, TEST_AGENT_CAL):
        events = bridge.list_events(window_start, today_start, calendar_titles=[cal])
        for e in events:
            print(f"Deleting {cal!r}: {e.start:%a %m-%d %H:%M}-{e.end:%H:%M} {e.title!r}")
            bridge.delete_event(e.identifier)
            total += 1

    # Completed test reminders from prior days are safe to purge too (open ones from a
    # prior day are left alone — they're not day-scoped the same way events are, and a
    # still-open reminder might be intentionally carried over for multi-day testing).
    reminders = bridge.list_reminders(list_titles=[TEST_REMINDER_LIST], include_completed=True)
    for r in reminders:
        if r.completed:
            print(f"Deleting completed reminder: {r.title!r}")
            bridge.delete_reminder(r.identifier)
            total += 1

    print(f"\nDeleted {total} stale item(s) from before {today_start:%Y-%m-%d}.")


if __name__ == "__main__":
    main()
