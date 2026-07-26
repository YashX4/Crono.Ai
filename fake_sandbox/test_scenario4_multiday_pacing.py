"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 4 (multi-day carryover: day-1 estimate -> day-2+ pacing), driven against a
single FakeEventKitBridge reused across two synthetic "days" (same store_path, same
reminder identifier — mirrors how a real EventKit reminder persists across days) rather
than scripts/setup_test_sandbox.run_seed().

Usage: .venv/bin/python fake_sandbox/test_scenario4_multiday_pacing.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario4-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.diff_write import decode_block_meta  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402
from timeblock_agent.task_progress import DEFAULT_TASK_PROGRESS_PATH, load_task_progress  # noqa: E402

trace = TraceLogger("test_scenario4_multiday_pacing")

DAY1 = datetime(2026, 7, 23, 9, 0)
DAY2 = datetime(2026, 7, 24, 9, 0)


def attempt():
    save_state(AgentState())
    if DEFAULT_TASK_PROGRESS_PATH.exists():
        DEFAULT_TASK_PROGRESS_PATH.unlink()

    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)

    # --- Day 1 ---
    bridge.create_event("Test Work", DAY1.replace(hour=9, minute=0), DAY1.replace(hour=10, minute=0), calendar_title="Test Bucket")
    epsilon_id = bridge.create_reminder(
        "Test task epsilon (~6 hours)", list_title="Test Reminders",
        notes="Finish the full project report — needs about 6 hours total.",
        due_date=DAY1.replace(hour=0, minute=0) + timedelta(days=3),
    )

    trace.call("handle_day_boundary", event="day_start", answer="yes", now=DAY1.isoformat())
    orchestrator.handle_day_boundary(bridge, "day_start", "yes", now=DAY1)
    trace.call("handle_goal_time_answer", level="none", now=DAY1.isoformat())
    orchestrator.handle_goal_time_answer(bridge, "none", now=DAY1)

    progress_after_day1 = load_task_progress()
    trace.check("TaskProgress entry exists for epsilon after day 1", True, epsilon_id in progress_after_day1)
    tp1 = progress_after_day1[epsilon_id]
    trace.check("day-1 completed_minutes is 0 (nothing done yet)", 0, tp1.completed_minutes)
    trace.check("day-1 due_date matches the reminder's own due date", DAY1.date() + timedelta(days=3), tp1.due_date)
    total_minutes = tp1.total_minutes
    trace.note(f"model's own day-1 realistic_total_minutes estimate for epsilon: {total_minutes}")

    # --- Day 2 --- (deliberately tight room: only Test Work's own 60-min window is
    # visible as future FLEXIBLE_BUCKET capacity through epsilon's due date, forcing
    # at_risk=True deterministically regardless of the model's exact day-1 estimate, as
    # long as it's anywhere near the "~6 hours" epsilon was framed as).
    bridge.create_event("Test Work", DAY2.replace(hour=9, minute=0), DAY2.replace(hour=10, minute=0), calendar_title="Test Bucket")

    orig_pacing = orchestrator._compute_task_pacing
    wrapper, calls = make_capturing_wrapper(orig_pacing)
    with mock.patch("timeblock_agent.orchestrator._compute_task_pacing", side_effect=wrapper):
        trace.call("handle_day_boundary", event="day_start", answer="yes", now=DAY2.isoformat())
        orchestrator.handle_day_boundary(bridge, "day_start", "yes", now=DAY2)
        trace.call("handle_goal_time_answer", level="none", now=DAY2.isoformat())
        orchestrator.handle_goal_time_answer(bridge, "none", now=DAY2)

    trace.check("_compute_task_pacing was called exactly once on day 2", 1, len(calls))
    pacing_by_id = calls[0]["result"]
    trace.check("epsilon has a pacing entry on day 2", True, epsilon_id in pacing_by_id)
    pacing = pacing_by_id[epsilon_id]

    remaining = total_minutes - tp1.completed_minutes
    days_remaining = 3  # due 2026-07-26, now 2026-07-24 -> (due - today).days + 1 == 3
    trace.check("days_remaining computed correctly", days_remaining, pacing.days_remaining)
    trace.check("epsilon is NOT overdue on day 2 (due date is still 2 days out)", False, pacing.overdue)
    trace.check(
        "epsilon is at_risk on day 2 (only ~60 min of future bucket capacity vs. hundreds remaining)",
        True, pacing.at_risk,
    )
    even_split = -(-remaining // days_remaining)  # same ceil-division idiom as _compute_task_pacing
    expected_target = max(int(min(remaining, even_split * 2)), 1)
    trace.check("today_target_minutes matches the at-risk front-loading formula", expected_target, pacing.today_target_minutes)

    # --- Check in "completed" on day 2's epsilon block -> partial credit, stays open ---
    day2_agent_blocks = bridge.list_events(
        DAY2.replace(hour=0, minute=0), DAY2.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar],
    )
    epsilon_block = next(
        (e for e in day2_agent_blocks if (m := decode_block_meta(e.notes)) and m.get("source_id") == epsilon_id), None
    )
    trace.check("epsilon got a task block placed on day 2", True, epsilon_block is not None)
    if epsilon_block is not None:
        session_minutes = int((epsilon_block.end - epsilon_block.start).total_seconds() / 60)
        checkin_now = DAY2 + timedelta(hours=2)
        trace.call("run_checkin_answer", block_id=epsilon_block.identifier, answer="completed", now=checkin_now.isoformat())
        orchestrator.run_checkin_answer(bridge, epsilon_block.identifier, "completed", now=checkin_now)

        progress_after_checkin = load_task_progress()
        trace.check("epsilon still tracked (not fully complete yet)", True, epsilon_id in progress_after_checkin)
        if epsilon_id in progress_after_checkin:
            tp2 = progress_after_checkin[epsilon_id]
            trace.check("completed_minutes incremented by exactly this session's duration", session_minutes, tp2.completed_minutes)
            trace.check("total_minutes unchanged by a completion", total_minutes, tp2.total_minutes)


def main():
    ok = True
    try:
        retry(3, attempt, trace, "Scenario 4 multi-day pacing")
        print("Scenario 4 (multi-day carryover pacing): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
