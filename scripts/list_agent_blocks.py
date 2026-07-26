"""Prints today's agent-authored task blocks with their EventKit identifiers, so you can
copy one for a manual webhook test.

Run with: .venv/bin/python scripts/list_agent_blocks.py
Must run from Terminal.app (not VS Code) for Calendar access.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.config import load_rules
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
    if not events:
        print(f"No agent-authored blocks found today in '{rules.agent_calendar}'.")
        return

    for e in events:
        print(f"{e.start:%H:%M}-{e.end:%H:%M} {e.title!r}\n  block_id: {e.identifier}\n")


if __name__ == "__main__":
    main()
