"""Manual live test: classifies today's remaining real calendar events via Claude.

Classifies twice in a row, in the same process — the pre-call classification cache
(see classify.py) is only ever in-memory for one process's lifetime, so re-running this
script as two separate invocations can never show a cache hit; the second pass has to
happen here, in-process, to actually exercise it the way a long-running server tick would.

Run with: .venv/bin/python scripts/test_classify.py
Must run from Terminal.app (not VS Code) for Calendar access to work.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from timeblock_agent.bucket_context import load_bucket_context
from timeblock_agent.classify import classify_events
from timeblock_agent.config import load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH
from timeblock_agent.scope import in_calendar_scope


def _print_result(events, result):
    for e in events:
        print(f"  {e.start:%H:%M}-{e.end:%H:%M} {e.title!r} ({e.calendar_title}) -> {result[e.identifier]}")


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
    print(f"{len(events)} in-scope events for the rest of today\n")

    if not events:
        print("(nothing left today)")
        return

    bucket_context = load_bucket_context()

    print("First pass (real API call expected):")
    result = classify_events(events, rules, bucket_context)
    _print_result(events, result)

    print("\nSecond pass, same process, nothing changed (should be served from cache — watch for the "
          "'fully served from cache — API call skipped' log line above):")
    result2 = classify_events(events, rules, bucket_context)
    _print_result(events, result2)

    print("\nThird pass, bucket_context changed (simulated in-memory — no file edit — by "
          "appending a space; should NOT be served from cache, a real API call expected again):")
    result3 = classify_events(events, rules, bucket_context + " ")
    _print_result(events, result3)


if __name__ == "__main__":
    main()
