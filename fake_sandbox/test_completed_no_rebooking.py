"""Tier C — needs the full env-var-before-import dance. Regression test for ISSUES.md
issue #27: `_resolve_agent_task` calls `replan_incremental` unconditionally regardless of
`answer`, including "completed" — but the reminder just marked "completed" is still
nominally "open" at that point (`_credit_reminder_completion`, which actually calls
`bridge.complete_reminder`, only runs a few lines LATER), so without a fix it remains a
legal candidate for the model to (re)schedule in the very same cycle.

Proves BOTH halves of the fix deterministically, with zero real API calls (see
test_goal_credit_edge_cases.py's own docstring for the same zero-cost pattern:
`bucket_blocks=[]`/a fully-mocked `replan_incremental` never reaches the network):

1. Root-cause fix: the just-completed reminder's own `source_id` is excluded from the
   `reminders` list `_resolve_agent_task` passes into `replan_incremental` for this one
   call, verified via a capturing wrapper around the REAL `replan_incremental` (same
   `make_capturing_wrapper` helper used elsewhere in this suite) — proven never to reach
   the model at all, which is the actually load-bearing guarantee here (a model can't
   propose rescheduling a candidate it was never shown). A contrast case with a
   non-"completed" answer confirms the exclusion is narrowly scoped to "completed" only.

2. Defense-in-depth fix: `completion_log.log_block` is always given the triggering task's
   ORIGINAL start/end, captured into local variables before `replan_incremental` (and the
   `apply_layout` call inside `_write_incremental_result`) ever runs — verified by
   mocking `replan_incremental`'s return value directly (no real model needed to
   "cooperate") to reuse the triggering task's own (bucket_event_id, source, source_id)
   key with a DIFFERENT start/end, which `apply_layout` then matches and applies via
   `bridge.update_event` onto the SAME calendar event id. Since
   `FakeEventKitBridge.update_event` mutates the same stored `CalendarEvent` object in
   place (`list_events` returns live references, not copies), this test confirms the
   mutation actually happened (the triggering_task object's own .start/.end changed) AND
   that the completion log still recorded the original, pre-mutation schedule.

Usage: .venv/bin/python fake_sandbox/test_completed_no_rebooking.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-completed-no-rebooking-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.completion_log import read_logs_between  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.diff_write import decode_block_meta, encode_block_meta  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.incremental_replan import IncrementalResult  # noqa: E402
from timeblock_agent.incremental_replan import replan_incremental as real_replan_incremental  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_completed_no_rebooking")


def case_exclusion_scoped_to_completed():
    """Half 1, contrast half: with a non-"completed" answer, the triggering reminder's
    own source_id is NOT excluded — proving the fix is narrowly scoped to "completed"
    only, not a blanket filter."""
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_contrast.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 12, 0)

    reminder_id = bridge.create_reminder("Test task beta (~20 min)", list_title="Test Reminders")
    other_reminder_id = bridge.create_reminder("Some other open reminder", list_title="Test Reminders")

    triggering_task = orchestrator.CalendarEvent(
        identifier="agent_block1", title="Test task beta (~20 min)",
        start=datetime(2026, 7, 23, 11, 0), end=datetime(2026, 7, 23, 11, 20),
        is_all_day=False, calendar_identifier="cal1", calendar_title=rules.agent_calendar,
        location=None, notes=None, url=None, has_attendees=False,
    )
    meta = {"bucket_event_id": "work1", "source": "reminder", "source_id": reminder_id}
    state = AgentState()

    wrapper, calls = make_capturing_wrapper(real_replan_incremental)
    with mock.patch("timeblock_agent.orchestrator.replan_incremental", side_effect=wrapper):
        trace.call(
            "_resolve_agent_task", answer="running_behind", source_id=reminder_id, bucket_blocks="[] (zero-cost short-circuit)",
        )
        orchestrator._resolve_agent_task(
            bridge, rules, state, now, triggering_task, meta, "running_behind", [], [], specific_events=[],
        )

    trace.check("exactly one replan_incremental call happened", 1, len(calls))
    reminders_passed = calls[0]["args"][4]
    ids_passed = {r.identifier for r in reminders_passed}
    trace.check(
        "on a non-'completed' answer, the triggering reminder is NOT excluded",
        True, reminder_id in ids_passed,
    )
    trace.check("the other open reminder is still present too", True, other_reminder_id in ids_passed)


def case_completed_excludes_own_reminder():
    """Half 1, main case: with answer == "completed", the reminder being closed out this
    exact cycle must never reach replan_incremental as a schedulable candidate, while
    OTHER still-open reminders remain untouched (this isn't a blanket wipe)."""
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_exclusion.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 12, 0)

    reminder_id = bridge.create_reminder("Test task beta (~20 min)", list_title="Test Reminders")
    other_reminder_id = bridge.create_reminder("Some other open reminder", list_title="Test Reminders")

    triggering_task = orchestrator.CalendarEvent(
        identifier="agent_block1", title="Test task beta (~20 min)",
        start=datetime(2026, 7, 23, 11, 0), end=datetime(2026, 7, 23, 11, 20),
        is_all_day=False, calendar_identifier="cal1", calendar_title=rules.agent_calendar,
        location=None, notes=None, url=None, has_attendees=False,
    )
    meta = {"bucket_event_id": "work1", "source": "reminder", "source_id": reminder_id}
    state = AgentState()

    wrapper, calls = make_capturing_wrapper(real_replan_incremental)
    with mock.patch("timeblock_agent.orchestrator.replan_incremental", side_effect=wrapper):
        trace.call(
            "_resolve_agent_task", answer="completed", source_id=reminder_id, bucket_blocks="[] (zero-cost short-circuit)",
        )
        orchestrator._resolve_agent_task(
            bridge, rules, state, now, triggering_task, meta, "completed", [], [], specific_events=[],
        )

    trace.check("exactly one replan_incremental call happened", 1, len(calls))
    reminders_passed = calls[0]["args"][4]
    ids_passed = {r.identifier for r in reminders_passed}
    trace.check(
        "the just-completed reminder's own source_id never reached replan_incremental",
        False, reminder_id in ids_passed,
    )
    trace.check(
        "a DIFFERENT still-open reminder is still present (not a blanket wipe)",
        True, other_reminder_id in ids_passed,
    )

    # Sanity: the reminder really was credited/completed via the normal path too.
    reloaded = bridge.list_reminders(include_completed=True)
    completed_reminder = next(r for r in reloaded if r.identifier == reminder_id)
    trace.check("the reminder itself was actually marked completed in Reminders.app", True, completed_reminder.completed)


def case_logging_survives_reused_identifier_mutation():
    """Half 2: even if replan_incremental's proposal reuses the triggering task's own
    (bucket_event_id, source, source_id) key with a NEW start/end — which apply_layout
    then applies via bridge.update_event onto the SAME calendar event id, mutating the
    live CalendarEvent object FakeEventKitBridge stores in place — completion_log.log_block
    must still record the block's ORIGINAL scheduled start/end, not the mutated one."""
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_logging.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 10, 0)

    scheduled_start = datetime(2026, 7, 23, 9, 0)
    scheduled_end = datetime(2026, 7, 23, 9, 20)
    reused_new_start = datetime(2026, 7, 23, 10, 0)
    reused_new_end = datetime(2026, 7, 23, 10, 20)

    (_tmp_dir / "logs" / f"{scheduled_start:%Y-%m-%d}.md").unlink(missing_ok=True)

    reminder_id = bridge.create_reminder("Test task beta (~20 min)", list_title="Test Reminders")
    bucket_event_id = "work1"
    seed_block = ProposedBlock(
        bucket_event_id=bucket_event_id, title="Test task beta (~20 min)", start=scheduled_start, end=scheduled_end,
        source="reminder", source_id=reminder_id,
    )
    identifier = bridge.create_event(
        title=seed_block.title, start=seed_block.start, end=seed_block.end,
        calendar_title=rules.agent_calendar, notes=encode_block_meta(seed_block),
    )
    # Fetch the SAME live object bridge.list_events hands back, matching exactly how
    # run_checkin_answer obtains triggering_task in production (never constructed fresh).
    triggering_task = next(
        e for e in bridge.list_events(
            scheduled_start.replace(hour=0, minute=0), scheduled_start.replace(hour=23, minute=59),
            calendar_titles=[rules.agent_calendar],
        )
        if e.identifier == identifier
    )
    meta = decode_block_meta(triggering_task.notes)
    state = AgentState()

    # Fully replace replan_incremental (no real model needed to "cooperate") with a
    # crafted proposal that reuses the triggering task's own key but a DIFFERENT
    # start/end — deterministically forcing the exact reused-identifier mutation
    # this issue describes, rather than depending on ~1/3 odds of a real Haiku call
    # happening to try it.
    fake_result = IncrementalResult(
        blocks=[
            ProposedBlock(
                bucket_event_id=bucket_event_id, title=triggering_task.title,
                start=reused_new_start, end=reused_new_end, source="reminder", source_id=reminder_id,
            )
        ],
        bucket_adjustments=[], unscheduled_reminder_ids=[],
    )
    with mock.patch("timeblock_agent.orchestrator.replan_incremental", return_value=fake_result):
        trace.call(
            "_resolve_agent_task", answer="completed", triggering_task_identifier=identifier,
            simulated_reproposal_start=reused_new_start.isoformat(), simulated_reproposal_end=reused_new_end.isoformat(),
        )
        orchestrator._resolve_agent_task(
            bridge, rules, state, now, triggering_task, meta, "completed", [], [], specific_events=[],
        )

    trace.check(
        "precondition: the SAME calendar event id got mutated to the NEW proposed start/end "
        "(confirms the aliasing scenario actually happened, not a no-op)",
        (reused_new_start, reused_new_end), (triggering_task.start, triggering_task.end),
    )

    entries = read_logs_between(scheduled_start.date(), scheduled_start.date())
    entry = next((e for e in entries if e["task"] == "Test task beta (~20 min)"), None)
    trace.check("a completion-log entry was written for the task", True, entry is not None)
    if entry is not None:
        trace.check(
            "logged start is the block's ORIGINAL scheduled start, NOT the mutated/reproposed one",
            scheduled_start, entry["start"],
        )
        trace.check(
            "logged end is the block's ORIGINAL scheduled end, NOT the mutated/reproposed one",
            scheduled_end, entry["end"],
        )
        trace.check("logged status is 'completed'", "completed", entry["status"])


def main():
    ok = True
    try:
        trace.step("Contrast case: non-'completed' answer does NOT exclude the reminder")
        case_exclusion_scoped_to_completed()

        trace.step("Main case: 'completed' answer excludes only its own reminder from replan_incremental")
        case_completed_excludes_own_reminder()

        trace.step("Logging case: completion_log uses the ORIGINAL schedule despite a reused-identifier mutation")
        case_logging_survives_reused_identifier_mutation()

        print("Completed-no-rebooking (issue #27) regression: PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
