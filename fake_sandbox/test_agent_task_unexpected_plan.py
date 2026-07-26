"""Tier C — needs the full env-var-before-import dance. Calls
orchestrator._resolve_agent_task directly for answer="unexpected_plan" — the sibling gap
flagged in ISSUES.md #28: an AGENT-authored task (not an external block like Gym)
answered "unexpected plan" went through the same replan_incremental machinery bug #28
found broken, and live testing (TESTING_LOG.md Session 5) confirmed it: 6 real Haiku
calls across two test beds, one with 40 genuinely spare minutes and a comfortably-fitting
reminder candidate, still 0 blocks proposed every time — the stale agent block just sat
there as if nothing had been said.

Fix (orchestrator.py, `_resolve_agent_task`): "unexpected_plan" on an agent task no
longer calls the model at all. It deletes the stale block via apply_layout (so
block_snapshots is cleaned up the same way every other system-initiated deletion already
is — otherwise the next weekly review would misread the deletion as a manual edit),
logs the resolution, and sets the followup. It deliberately does NOT try to pick/size a
replacement reminder for the freed time — that's a genuine judgment call (closer to
day_layout.plan_day's own job), left for a future pass rather than guessed at here.
"completed"/"running_behind" are untouched — still go through replan_incremental as before.

Usage: .venv/bin/python fake_sandbox/test_agent_task_unexpected_plan.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-agent-unexpected-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.block_snapshots import load_snapshots  # noqa: E402
from timeblock_agent.completion_log import read_logs_between  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.diff_write import apply_layout, decode_block_meta  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_agent_task_unexpected_plan")


def _place_agent_task(bridge, rules, bucket_id, title, start, end, source_id, now):
    block = ProposedBlock(bucket_event_id=bucket_id, title=title, start=start, end=end, source="reminder", source_id=source_id)
    apply_layout(bridge, [block], existing_agent_events=[], agent_calendar=rules.agent_calendar, written_at=now)
    events = bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar])
    return next(e for e in events if e.title == title)


def case_a_deletes_stale_block_no_model_call():
    trace.step("Case A: 'unexpected_plan' on an agent task deletes the stale block, calls the model zero times")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 14, 35)

    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 23, 14, 0), datetime(2026, 7, 23, 17, 0), calendar_title="Test Bucket")
    gamma_id = bridge.create_reminder("Test task gamma", list_title="Test Reminders")
    gamma_block = _place_agent_task(
        bridge, rules, hobby_id, "Test task gamma", datetime(2026, 7, 23, 14, 0), datetime(2026, 7, 23, 14, 15), gamma_id, now,
    )
    trace.note(f"Placed agent block {gamma_block.identifier} for 'Test task gamma'")

    hobby_event = next(iter(bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=["Test Bucket"])))
    meta = decode_block_meta(gamma_block.notes)

    state = AgentState()
    wrapper, calls = make_capturing_wrapper(orchestrator.replan_incremental)
    with mock.patch("timeblock_agent.orchestrator.replan_incremental", side_effect=wrapper):
        trace.call("_resolve_agent_task", triggering_task="Test task gamma", answer="unexpected_plan", now=now.isoformat())
        orchestrator._resolve_agent_task(
            bridge, rules, state, now, gamma_block, meta, "unexpected_plan", [hobby_event], fixed_events=[], specific_events=[],
        )

    trace.check("replan_incremental was NEVER called (deterministic, no model call)", 0, len(calls))

    remaining = bridge.list_events(
        now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar],
    )
    trace.check("the stale agent block is gone", True, all(e.identifier != gamma_block.identifier for e in remaining))

    snapshots = load_snapshots()
    trace.check("its block_snapshots entry was also cleaned up (no false manual-edit flag later)", False, gamma_block.identifier in snapshots)

    trace.check("pending_followup_reason is 'unexpected'", "unexpected", state.pending_followup_reason)
    trace.check(
        "pending_followup_at set per followup_delay_unexpected_minutes",
        now + timedelta(minutes=rules.followup_delay_unexpected_minutes), state.pending_followup_at,
    )

    entries = read_logs_between(now.date(), now.date())
    entry = next((e for e in entries if e["task"] == "Test task gamma"), None)
    trace.check("completion log entry written", True, entry is not None)
    if entry is not None:
        trace.check("logged status is 'bumped' (unexpected_plan)", "bumped", entry["status"])
        trace.check("logged source is 'reminder' (this task's own source, not 'external')", "reminder", entry["source"])


def case_b_reminder_stays_open_for_a_future_pass():
    trace.step("Case B: the underlying reminder is untouched (not completed, not deleted) — free to be picked up later")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 24, 14, 35)  # own date — avoids sharing a log file with Case A

    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 24, 14, 0), datetime(2026, 7, 24, 17, 0), calendar_title="Test Bucket")
    gamma_id = bridge.create_reminder("Test task gamma", list_title="Test Reminders")
    gamma_block = _place_agent_task(
        bridge, rules, hobby_id, "Test task gamma", datetime(2026, 7, 24, 14, 0), datetime(2026, 7, 24, 14, 15), gamma_id, now,
    )
    hobby_event = next(iter(bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=["Test Bucket"])))
    meta = decode_block_meta(gamma_block.notes)

    state = AgentState()
    orchestrator._resolve_agent_task(
        bridge, rules, state, now, gamma_block, meta, "unexpected_plan", [hobby_event], fixed_events=[], specific_events=[],
    )

    reminders = bridge.list_reminders(include_completed=True)
    reminder = next((r for r in reminders if r.identifier == gamma_id), None)
    trace.check("the underlying reminder still exists", True, reminder is not None)
    if reminder is not None:
        trace.check("it was never marked completed", False, reminder.completed)


def case_c_completed_and_running_behind_untouched():
    trace.step("Case C: 'completed' and 'running_behind' still go through replan_incremental as before (regression guard)")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 22, 14, 35)  # own date, same reason as Case B

    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 22, 14, 0), datetime(2026, 7, 22, 17, 0), calendar_title="Test Bucket")

    for answer in ("completed", "running_behind"):
        gamma_id = bridge.create_reminder(f"Test task {answer}", list_title="Test Reminders")
        gamma_block = _place_agent_task(
            bridge, rules, hobby_id, f"Test task {answer}", datetime(2026, 7, 22, 14, 0), datetime(2026, 7, 22, 14, 15), gamma_id, now,
        )
        hobby_event = next(iter(bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=["Test Bucket"])))
        meta = decode_block_meta(gamma_block.notes)

        state = AgentState()
        wrapper, calls = make_capturing_wrapper(orchestrator.replan_incremental)
        with mock.patch("timeblock_agent.orchestrator.replan_incremental", side_effect=wrapper):
            orchestrator._resolve_agent_task(
                bridge, rules, state, now, gamma_block, meta, answer, [hobby_event], fixed_events=[], specific_events=[],
            )
        trace.check(f"'{answer}' still calls replan_incremental exactly once (untouched by this fix)", 1, len(calls))


def main():
    ok = True
    cases = [
        ("Case A (deletes stale block, zero model calls)", case_a_deletes_stale_block_no_model_call),
        ("Case B (reminder stays open)", case_b_reminder_stays_open_for_a_future_pass),
        ("Case C (completed/running_behind untouched)", case_c_completed_and_running_behind_untouched),
    ]
    try:
        for label, fn in cases:
            fn()
            print(f"{label}: PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
