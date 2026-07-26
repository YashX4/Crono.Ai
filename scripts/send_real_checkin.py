"""Sends a REAL actionable check-in notification for a real checkable block — an
agent-authored task, a real FIXED commitment (meeting), or an already-specific personal
block (Gym, Lunch) — bypassing the wait for its actual trigger time. Lets you test the
full loop (tap on phone -> real replan -> real calendar update) without waiting for the
natural trigger.

Looks the block up via EventKit directly (needs Terminal.app for Calendar access) across
EVERY in-scope calendar today, not just the dedicated agent calendar — a FIXED/SPECIFIC
block lives in your own real calendars, not "Task Blocks". Routes the actual Telegram
send through the running server's /debug/send-test-checkin endpoint — the button-tap
token only exists in whichever process's memory registered it, so sending directly from
this script (a separate process from the server) would leave the buttons unable to
resolve when tapped, even with the server's poll loop running.

Usage: .venv/bin/python scripts/send_real_checkin.py <block_id>
Get a real block_id from scripts/list_agent_blocks.py (agent-authored tasks) or
scripts/list_today_events.py (everything, including FIXED/SPECIFIC). The server must be
running.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

from timeblock_agent.config import load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH
from timeblock_agent.scope import in_calendar_scope

load_dotenv()


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <block_id>")
        return
    block_id = sys.argv[1]

    webhook_token = os.environ.get("WEBHOOK_TOKEN")
    if not webhook_token:
        print("WEBHOOK_TOKEN not set in .env")
        return

    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()
    cal_granted, _ = bridge.request_access()
    if not cal_granted:
        print("Calendar access not granted.")
        return

    now = datetime.now()
    events = bridge.list_events(now.replace(hour=0, minute=0, second=0), now.replace(hour=23, minute=59, second=59))
    # in_calendar_scope alone is wrong here: rules.agent_calendar is a dedicated
    # destination for agent-authored blocks, separate from the calendars.include/exclude
    # scope used for bucket classification — a test config that scopes calendars.include
    # to just "Test Bucket" would otherwise make every agent-authored block invisible to
    # this script (caught live: send_real_checkin.py couldn't find its own task blocks).
    events = [e for e in events if in_calendar_scope(e.calendar_title, rules) or e.calendar_title == rules.agent_calendar]
    block = next((e for e in events if e.identifier == block_id), None)
    if block is None:
        print(f"No in-scope event with id {block_id} found today.")
        return

    print(f"Sending real check-in for {block.title!r} ({block.start:%H:%M}-{block.end:%H:%M})...")
    try:
        response = httpx.post(
            "http://localhost:8787/debug/send-test-checkin",
            json={
                "token": webhook_token,
                "block_id": block.identifier,
                "title": f"{block.title!r} check-in",
                "text": f"Was supposed to end at {block.end:%H:%M} — done, still going, or something unexpected come up?",
            },
            timeout=15,
        )
    except httpx.ConnectError:
        print("Couldn't reach the server on localhost:8787 — is it running?")
        return

    print(f"HTTP {response.status_code}: {response.json()}")
    if response.status_code == 200:
        print("Sent. Check your phone and tap a button.")


if __name__ == "__main__":
    main()
