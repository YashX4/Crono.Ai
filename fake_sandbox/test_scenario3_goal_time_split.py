"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 3 (goal-time sourcing: Hobby fallback + multi-goal split), driven against a
deterministic hand-built FakeEventKitBridge (never scripts/setup_test_sandbox.run_seed()
— see EDGE_CASES.md for why).

Two separate cases, deliberately NOT combined in one bridge: an earlier combined version
of this test put Test Buffer in the same seed as the Hobby-fallback scenario, and Test
Buffer — also tagged NOT goal-time-eligible by the model, same as Test Hobby — became a
second, tiny (20-min) candidate donor bucket that sometimes got picked over Test Hobby,
an artifact of this test's own setup (a real, unrelated donor-selection concern doesn't
apply generally — production only ever has ONE non-eligible bucket in the real sandbox
shape), not a product bug. Splitting the two concerns removes the confound.

Usage: .venv/bin/python fake_sandbox/test_scenario3_goal_time_split.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario3-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import decode_goal_time_meta  # noqa: E402
from timeblock_agent.diff_write import decode_block_meta  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario3_goal_time_split")

_GOAL_A = """\
---
title: Test goal A
priority: medium
status: active
last_touched: 2020-01-01
next_action: Do test goal A's next action
---

## Notes

## Log
<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->
"""

_GOAL_B = """\
---
title: Test goal B
priority: low
status: active
last_touched: 2020-06-01
next_action: Do test goal B's next action
---

## Notes

## Log
<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->
"""


def _write_goals():
    goals_dir = _tmp_dir / "goals"
    for f in goals_dir.glob("*.md"):
        f.unlink()
    (goals_dir / "test-goal-a.md").write_text(_GOAL_A)
    (goals_dir / "test-goal-b.md").write_text(_GOAL_B)


def hobby_fallback_and_multi_split_attempt():
    save_state(AgentState())
    _write_goals()
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    now = datetime(2026, 7, 23, 9, 0)
    rules = load_rules(RULES_PATH)

    bridge.create_event("Test Work", now.replace(hour=9, minute=0), now.replace(hour=11, minute=0), calendar_title="Test Bucket")
    bridge.create_event("Test Meeting", now.replace(hour=10, minute=30), now.replace(hour=10, minute=45), calendar_title="Test Bucket")
    bridge.create_event("Test Hobby", now.replace(hour=11, minute=0), now.replace(hour=14, minute=0), calendar_title="Test Bucket")
    # Sized to fit cleanly BEFORE the nested meeting (09:00-10:30, 90 min) — leaves only
    # ~15 min of real slack in Test Work (window 120 min - 90 min task - 15 min meeting),
    # nowhere near the 150-min "Balanced" ask, deterministically forcing the
    # shrink-fallback branch onto Test Hobby (the ONLY other bucket here, so there's no
    # ambiguity about which bucket becomes the donor).
    bridge.create_reminder("Test task big (~90 min)", list_title="Test Reminders", notes="Write a 90-minute report.")

    orig_source_goal_time = orchestrator._source_goal_time
    wrapper, calls = make_capturing_wrapper(orig_source_goal_time)
    with mock.patch("timeblock_agent.orchestrator._source_goal_time", side_effect=wrapper):
        trace.call("handle_day_boundary", event="day_start", answer="yes", now=now.isoformat())
        orchestrator.handle_day_boundary(bridge, "day_start", "yes", now=now)
        trace.call("handle_goal_time_answer", level="balanced", now=now.isoformat())
        orchestrator.handle_goal_time_answer(bridge, "balanced", now=now)

    trace.check("_source_goal_time was called exactly once", 1, len(calls))
    bucket_blocks_seen, goal_time_bucket = calls[0]["result"]
    trace.check("a Goal Time bucket was sourced", True, goal_time_bucket is not None)
    donor_id = decode_goal_time_meta(goal_time_bucket.notes) if goal_time_bucket else None
    donor = next((b for b in bucket_blocks_seen if b.identifier == donor_id), None)
    trace.check("Goal Time sourced from Test Hobby (Test Work had no slack)", "Test Hobby", donor.title if donor else None)
    trace.check(
        "Goal Time bucket spans exactly 150 min (the full 'Balanced' budget)",
        150, int((goal_time_bucket.end - goal_time_bucket.start).total_seconds() / 60),
    )

    goal_blocks = sorted(
        (
            e for e in bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar])
            if e.title in ("Do test goal A's next action", "Do test goal B's next action")
        ),
        key=lambda e: e.start,
    )
    trace.check("exactly 2 goal-session blocks landed on the calendar (multi-goal split)", 2, len(goal_blocks))
    durations = [int((e.end - e.start).total_seconds() / 60) for e in goal_blocks]
    trace.check("goal-session durations sum to the full 150-min budget", 150, sum(durations))
    trace.check("no single goal-session exceeds max_minutes_per_goal (90)", True, all(d <= 90 for d in durations))
    trace.check("durations are exactly {90, 60} (goal A capped at 90, goal B gets the 60 remainder)", {90, 60}, set(durations))


def buffer_never_gets_reminder_attempt():
    save_state(AgentState())
    _write_goals()
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    now = datetime(2026, 7, 23, 9, 0)
    rules = load_rules(RULES_PATH)

    bridge.create_event("Test Work", now.replace(hour=9, minute=0), now.replace(hour=11, minute=0), calendar_title="Test Bucket")
    bridge.create_event("Test Buffer", now.replace(hour=11, minute=0), now.replace(hour=11, minute=30), calendar_title="Test Bucket")
    # A small reminder gives the model a genuine opportunity to (wrongly) try placing it
    # in the protected Test Buffer, so the invariant is actually exercised rather than
    # vacuously true from having nothing else around to place. goal_time=none — this
    # case is deliberately unrelated to the goal-time carve-out mechanic entirely.
    bridge.create_reminder("Test task small (~15 min)", list_title="Test Reminders", notes="A quick 15-minute check.")

    trace.call("handle_day_boundary", event="day_start", answer="yes", now=now.isoformat())
    orchestrator.handle_day_boundary(bridge, "day_start", "yes", now=now)
    trace.call("handle_goal_time_answer", level="none", now=now.isoformat())
    orchestrator.handle_goal_time_answer(bridge, "none", now=now)

    buffer_bucket = next(
        b for b in bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=["Test Bucket"])
        if b.title == "Test Buffer"
    )
    reminder_in_buffer = [
        e for e in bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar])
        if (m := decode_block_meta(e.notes)) and m.get("bucket_event_id") == buffer_bucket.identifier and m.get("source") == "reminder"
    ]
    trace.check("Test Buffer (protected) never received a reminder-sourced block", [], reminder_in_buffer)


def main():
    ok = True
    try:
        retry(3, hobby_fallback_and_multi_split_attempt, trace, "Hobby fallback + multi-goal split")
        print("Case 1 (Hobby fallback + multi-goal split): PASS")
        retry(3, buffer_never_gets_reminder_attempt, trace, "Buffer never gets a reminder")
        print("Case 2 (protected Test Buffer never gets a reminder): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
