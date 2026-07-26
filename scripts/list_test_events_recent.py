"""Lists every event in the sandbox test calendars ("Test Bucket", "Task Blocks (Test)")
across the last N days (default 2) — unlike list_today_events.py/list_agent_blocks.py,
which only ever look at today. Useful for spotting stale leftover test events from a
previous session that setup_test_sandbox.py's wipe never touches, since that wipe is
always scoped to whatever "today" is at the moment it's run, never earlier days.

Usage: .venv/bin/python scripts/list_test_events_recent.py [--days N]
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.eventkit_bridge import EventKitBridge

TEST_CALENDARS = ["Test Bucket", "Task Blocks (Test)"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="How many days back to look (default 2)")
    args = parser.parse_args()

    bridge = EventKitBridge()
    cal_granted, rem_granted = bridge.request_access()
    if not cal_granted:
        print("Calendar access not granted.")
        return

    now = datetime.now()
    start = (now - timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    for cal in TEST_CALENDARS:
        print(f"--- {cal} ---")
        events = bridge.list_events(start, end, calendar_titles=[cal])
        if not events:
            print("  (none)")
            continue
        for e in sorted(events, key=lambda e: e.start):
            print(f"  {e.start:%a %m-%d %H:%M}-{e.end:%H:%M}  {e.title!r}  [{e.identifier}]")


if __name__ == "__main__":
    main()
