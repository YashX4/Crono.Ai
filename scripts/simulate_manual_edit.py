"""Simulates a manual Calendar.app edit (retitle and/or resize) on an agent-authored
block, for testing the weekly review's silent manual-edit detection (see
block_snapshots.py, weekly_review.py, TESTING.md).

A direct EventKit write here, bypassing diff_write.apply_layout entirely, is
indistinguishable from a real hand-edit from the system's point of view — it only ever
tracks whether its OWN write path touched a block since the last snapshot, never who or
what actually made the change.

Usage:
  .venv/bin/python scripts/simulate_manual_edit.py <block_id> --title "New Title"
  .venv/bin/python scripts/simulate_manual_edit.py <block_id> --extend 30
  .venv/bin/python scripts/simulate_manual_edit.py <block_id> --delete
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.config import load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("block_id")
    parser.add_argument("--title", help="New title (simulates a manual retitle)")
    parser.add_argument("--extend", type=int, help="Extend the block's end by N minutes (simulates a manual resize)")
    parser.add_argument("--delete", action="store_true", help="Delete the block entirely (simulates a manual deletion)")
    args = parser.parse_args()

    if not args.title and not args.extend and not args.delete:
        print("Nothing to do — pass --title, --extend, or --delete.")
        return

    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()
    cal_granted, _ = bridge.request_access()
    if not cal_granted:
        print("Calendar access not granted.")
        return

    now = datetime.now()
    events = bridge.list_events(
        now.replace(hour=0, minute=0, second=0), now.replace(hour=23, minute=59, second=59),
        calendar_titles=[rules.agent_calendar],
    )
    block = next((e for e in events if e.identifier == args.block_id), None)
    if block is None:
        print(f"No block with id {args.block_id} found in {rules.agent_calendar} today.")
        return

    if args.delete:
        print(f"Deleting {block.title!r} ({block.start:%H:%M}-{block.end:%H:%M})...")
        bridge.delete_event(block.identifier)
        print("Done.")
        return

    kwargs = {}
    if args.title:
        kwargs["title"] = args.title
    if args.extend:
        kwargs["end"] = block.end + timedelta(minutes=args.extend)

    print(f"Editing {block.title!r} ({block.start:%H:%M}-{block.end:%H:%M}) -> {kwargs}")
    bridge.update_event(block.identifier, **kwargs)
    print("Done — this now looks like a manual edit to the system.")


if __name__ == "__main__":
    main()
