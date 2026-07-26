"""Tier C — needs the full env-var-before-import dance (imports timeblock_agent.orchestrator).
Automated version of TESTING.md Scenario 2 (day-start confirmation + realistic sizing +
bucket extension), driven against a deterministic hand-built FakeEventKitBridge rather
than scripts/setup_test_sandbox.run_seed() (whose layout is computed relative to real
`now` and would make exact assertions flaky depending on time of day — see EDGE_CASES.md).

Usage: .venv/bin/python fake_sandbox/test_scenario2_day_start_sizing.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario2-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import day_layout, orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario2_day_start_sizing")


def seed_bridge(bridge, now):
    bridge.create_event("Test Work", now.replace(hour=9, minute=0), now.replace(hour=11, minute=0), calendar_title="Test Bucket")
    bridge.create_event("Test Meeting", now.replace(hour=10, minute=30), now.replace(hour=10, minute=45), calendar_title="Test Bucket")
    bridge.create_event("Test Hobby", now.replace(hour=11, minute=0), now.replace(hour=14, minute=0), calendar_title="Test Bucket")
    bridge.create_reminder("Test task alpha (~20 min)", list_title="Test Reminders", notes="Quick email to send.")
    bridge.create_reminder("Test task beta (~20 min)", list_title="Test Reminders", notes="Update the resume.")
    bridge.create_reminder("Test task gamma (~30 min)", list_title="Test Reminders", notes="Review pending messages.")
    bridge.create_reminder("Test task delta (~90 min)", list_title="Test Reminders", notes="Write the first draft of a report.")


def attempt():
    save_state(AgentState())  # fresh state every attempt
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    now = datetime(2026, 7, 23, 9, 0)
    seed_bridge(bridge, now)
    rules = load_rules(RULES_PATH)

    wrapper, calls = make_capturing_wrapper(day_layout.plan_day)
    with mock.patch("timeblock_agent.orchestrator.plan_day", side_effect=wrapper):
        trace.call("handle_day_boundary", event="day_start", answer="yes", now=now.isoformat())
        orchestrator.handle_day_boundary(bridge, "day_start", "yes", now=now)
        trace.call("handle_goal_time_answer", level="none", now=now.isoformat())
        orchestrator.handle_goal_time_answer(bridge, "none", now=now)

    trace.check("plan_day was called exactly once", 1, len(calls))
    result = calls[0]["result"]

    reminder_titles = {"Test task alpha (~20 min)", "Test task beta (~20 min)", "Test task gamma (~30 min)"}
    reminders = bridge.list_reminders(list_titles=["Test Reminders"])
    reminders_by_title = {r.title: r for r in reminders}

    scheduled_minutes: dict[str, float] = {}
    for b in result.blocks:
        if b.source == "reminder":
            scheduled_minutes[b.source_id] = scheduled_minutes.get(b.source_id, 0) + (b.end - b.start).total_seconds() / 60
    estimates_by_id = {e.reminder_id: e for e in result.task_estimates}

    for title in reminder_titles:
        r = reminders_by_title[title]
        est = estimates_by_id.get(r.identifier)
        scheduled = scheduled_minutes.get(r.identifier, 0)
        is_unscheduled = r.identifier in result.unscheduled_reminder_ids
        not_squashed = est is None or scheduled >= est.realistic_total_minutes * 0.75
        trace.check(
            f"{title!r} is either not squashed (>=75% of its own claimed size) or explicitly unscheduled",
            True, not_squashed or is_unscheduled,
        )

    delta = reminders_by_title["Test task delta (~90 min)"]
    delta_scheduled = scheduled_minutes.get(delta.identifier, 0)
    delta_unscheduled = delta.identifier in result.unscheduled_reminder_ids
    work_extended = len(result.bucket_adjustments) > 0
    trace.check(
        "delta (~90 min, deliberately overflowing) triggered a bucket extension, was scheduled, or fell to unscheduled",
        True, work_extended or delta_scheduled > 0 or delta_unscheduled,
    )

    meeting_start, meeting_end = datetime(2026, 7, 23, 10, 30), datetime(2026, 7, 23, 10, 45)
    for b in result.blocks:
        overlap = b.start < meeting_end and b.end > meeting_start
        trace.check(f"block {b.title!r} does not overlap Test Meeting (10:30-10:45)", False, overlap)


def main():
    ok = True
    try:
        retry(3, attempt, trace, "Scenario 2 day-start sizing")
        print("Scenario 2 (day-start sizing + extension): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
