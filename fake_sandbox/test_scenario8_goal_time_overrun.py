"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 8 (Goal Time eaten first on overrun) — the automated version of today's manual
live/fake-sandbox run: Goal Time sourced from Test Work's own natural slack (immediately
adjacent to the triggering task, zero gap), then repeated "still going" rounds on that
task, asserting Goal Time unconditionally shrinks by the exact same amount each round
(bug #20) and its own goal-session task block stays in sync with the bucket's current
front every round (bugs #22/#23).

Usage: .venv/bin/python fake_sandbox/test_scenario8_goal_time_overrun.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario8-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock, decode_goal_time_meta  # noqa: E402
from timeblock_agent.diff_write import apply_layout, decode_block_meta  # noqa: E402
from timeblock_agent.goals import list_goals  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario8_goal_time_overrun")

_GOAL_A = """\
---
title: Test goal A
priority: high
status: active
last_touched: 2020-01-01
next_action: Do test goal A's next action
---

## Notes

## Log
<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->
"""

WORK_START = datetime(2026, 7, 23, 9, 0)
BETA_END = datetime(2026, 7, 23, 9, 40)
WORK_END = datetime(2026, 7, 23, 10, 0)
GOAL_TIME_MINUTES = 20


def attempt():
    save_state(AgentState())
    (_tmp_dir / "goals" / "test-goal-a.md").write_text(_GOAL_A)
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)

    work_id = bridge.create_event("Test Work", WORK_START, WORK_END, calendar_title="Test Bucket")
    reminder_id = bridge.create_reminder("Test task beta", list_title="Test Reminders")
    beta_block = ProposedBlock(
        bucket_event_id=work_id, title="Test task beta", start=WORK_START, end=BETA_END,
        source="reminder", source_id=reminder_id,
    )

    work_event = next(
        e for e in bridge.list_events(WORK_START.replace(hour=0, minute=0), WORK_START.replace(hour=23, minute=59), calendar_titles=["Test Bucket"])
        if e.identifier == work_id
    )
    bucket_blocks, goal_time_bucket = orchestrator._source_goal_time(
        bridge, rules, [work_event], [beta_block], {work_id: True}, GOAL_TIME_MINUTES, fixed_events=[], specific_events=[],
    )
    trace.check("Goal Time was sourced (natural slack, no shrink needed)", True, goal_time_bucket is not None)
    trace.check("Goal Time sourced from Test Work", work_id, decode_goal_time_meta(goal_time_bucket.notes))
    trace.check("Goal Time starts immediately where beta ends (zero gap)", BETA_END, goal_time_bucket.start)
    trace.check("Goal Time spans exactly the requested 20 min", WORK_END, goal_time_bucket.end)

    goals = list_goals()
    goal_session_blocks = orchestrator._build_goal_session_blocks(
        goal_time_bucket, rules, goals, GOAL_TIME_MINUTES, WORK_START, fixed_events=[]
    )
    trace.check("exactly one goal-session block built (single goal, budget fits under its cap)", 1, len(goal_session_blocks))

    apply_layout(
        bridge, [beta_block] + goal_session_blocks, existing_agent_events=[], agent_calendar=rules.agent_calendar,
        written_at=WORK_START,
    )

    agent_events = bridge.list_events(
        WORK_START.replace(hour=0, minute=0), WORK_START.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar]
    )
    beta_event = next(e for e in agent_events if (m := decode_block_meta(e.notes)) and m.get("source_id") == reminder_id)
    stable_beta_id = beta_event.identifier

    now = BETA_END
    expected_beta_end = now
    expected_goal_time_start = goal_time_bucket.start
    for round_num in range(1, 4):
        trace.call("run_checkin_answer", block_id=stable_beta_id, answer="running_behind", now=now.isoformat())
        orchestrator.run_checkin_answer(bridge, stable_beta_id, "running_behind", now=now)

        agent_events = bridge.list_events(
            WORK_START.replace(hour=0, minute=0), WORK_START.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar]
        )
        beta_current = next(e for e in agent_events if e.identifier == stable_beta_id)
        expected_beta_end = expected_beta_end + timedelta(minutes=rules.followup_delay_continuing_minutes)
        trace.check(f"round {round_num}: beta extended by followup_delay_continuing_minutes", expected_beta_end, beta_current.end)

        bucket_events = bridge.list_events(
            WORK_START.replace(hour=0, minute=0), WORK_START.replace(hour=23, minute=59), calendar_titles=["Test Bucket"]
        )
        goal_time_current = next(e for e in bucket_events if e.title == "Goal Time")
        expected_goal_time_start = expected_goal_time_start + timedelta(minutes=rules.followup_delay_continuing_minutes)
        trace.check(
            f"round {round_num}: Goal Time shrunk from the front by the same amount beta extended (bug #20)",
            expected_goal_time_start, goal_time_current.start,
        )
        trace.check(f"round {round_num}: Goal Time's own end never changed", WORK_END, goal_time_current.end)

        goal_session_current = next(
            e for e in agent_events
            if (m := decode_block_meta(e.notes)) and m.get("bucket_event_id") == goal_time_bucket.identifier
        )
        trace.check(
            f"round {round_num}: goal-session task block's start stays in sync with Goal Time's new front (bugs #22/#23)",
            expected_goal_time_start, goal_session_current.start,
        )

        now = beta_current.end


def main():
    ok = True
    try:
        retry(3, attempt, trace, "Scenario 8 goal-time overrun")
        print("Scenario 8 (Goal Time eaten first on overrun): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
