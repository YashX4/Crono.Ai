"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 7 ("still going" — extension + priority cascade), driven through the FULL
run_checkin_answer -> apply_layout stack across several repeated rounds (not a single
direct replan_incremental call, unlike test_priority_cascade_end_to_end.py's Tier B
precision-numeric check) — the distinct value here is confirming the SAME calendar
event gets UPDATED, never recreated, across many consecutive "still going" rounds (bug
#24's exact regression guard, now automated rather than manually eyeballed).

Usage: .venv/bin/python fake_sandbox/test_scenario7_still_going_cascade.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario7-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.diff_write import apply_layout  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario7_still_going_cascade")

# Meeting sits at the very FRONT of Test Work (matching this session's own established
# compact-mode convention) — a single contiguous task block can never validly span
# across a FIXED event, so placing the meeting anywhere but the front would permanently
# cap the cascade there; at the front, the triggering task simply starts right after it.
MEETING_START, MEETING_END = datetime(2026, 7, 23, 9, 0), datetime(2026, 7, 23, 9, 3)
WORK_END = datetime(2026, 7, 23, 10, 0)
HOBBY_END = datetime(2026, 7, 23, 13, 0)


def attempt():
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)

    work_id = bridge.create_event("Test Work", MEETING_START, WORK_END, calendar_title="Test Bucket")
    bridge.create_event("Test Meeting", MEETING_START, MEETING_END, calendar_title="Test Bucket")
    hobby_id = bridge.create_event("Test Hobby", WORK_END, HOBBY_END, calendar_title="Test Bucket")
    reminder_id = bridge.create_reminder("Test task beta", list_title="Test Reminders")

    # Triggering task fills Work exactly, right after the meeting — zero natural slack.
    block = ProposedBlock(
        bucket_event_id=work_id, title="Test task beta", start=MEETING_END, end=WORK_END,
        source="reminder", source_id=reminder_id,
    )
    apply_layout(bridge, [block], existing_agent_events=[], agent_calendar=rules.agent_calendar, written_at=MEETING_END)
    agent_block = next(iter(bridge.list_events(
        MEETING_START.replace(hour=0, minute=0), MEETING_START.replace(hour=23, minute=59),
        calendar_titles=[rules.agent_calendar],
    )))
    stable_id = agent_block.identifier

    now = WORK_END
    expected_end = now
    for round_num in range(1, 4):
        trace.call("run_checkin_answer", block_id=stable_id, answer="running_behind", now=now.isoformat())
        orchestrator.run_checkin_answer(bridge, stable_id, "running_behind", now=now)

        agent_events = bridge.list_events(
            MEETING_START.replace(hour=0, minute=0), MEETING_START.replace(hour=23, minute=59),
            calendar_titles=[rules.agent_calendar],
        )
        matching = [e for e in agent_events if e.identifier == stable_id]
        trace.check(f"round {round_num}: same calendar event id persists (bug #24 regression guard)", 1, len(matching))
        current = matching[0]
        expected_end = expected_end + timedelta(minutes=rules.followup_delay_continuing_minutes)
        trace.check(f"round {round_num}: extended by followup_delay_continuing_minutes", expected_end, current.end)
        trace.check(f"round {round_num}: start never moved", MEETING_END, current.start)

        overlap = current.start < MEETING_END and current.end > MEETING_START
        trace.check(f"round {round_num}: never overlaps Test Meeting", False, overlap)

        now = current.end

    hobby_events = bridge.list_events(
        MEETING_START.replace(hour=0, minute=0), MEETING_START.replace(hour=23, minute=59), calendar_titles=["Test Bucket"],
    )
    hobby = next(e for e in hobby_events if e.identifier == hobby_id)
    trace.check("Test Hobby shrunk from the front by the total overrun (matching Work's total extension)", expected_end, hobby.start)
    trace.check("Test Hobby's own end never changed", HOBBY_END, hobby.end)


def main():
    ok = True
    try:
        # Higher bound than most other bounded-retry cases here: this attempt requires
        # the SAME correct priority judgment (Hobby yields to Work) to land independently
        # on 3 consecutive rounds, not just once — the compounding effect meaningfully
        # lowers the whole attempt's pass rate per try compared to a single-round check
        # (confirmed directly: 3 attempts was occasionally not enough in practice).
        retry(5, attempt, trace, "Scenario 7 still-going cascade")
        print("Scenario 7 ('still going' extension + priority cascade): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
