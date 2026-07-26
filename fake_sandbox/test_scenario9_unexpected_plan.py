"""Tier C — needs the full env-var-before-import dance. Calls
orchestrator._resolve_external_block directly — Scenario 9's own function.

Rewritten after live testing (TESTING_LOG.md) found the ORIGINAL model-driven cascade
(a "unexpected_plan" replan_incremental call retargeted at the next bucket) reliably
proposed nothing — 9 consecutive real Haiku calls across multiple prompt rewrites, all
0 blocks/0 bucket_adjustments, even with a genuinely available reminder and ample room.
Replaced with a deterministic, code-only shrink of the next bucket's own front
(_find_deterministic_cascade_target in orchestrator.py) — no model call, no
non-determinism, so this suite no longer needs make_capturing_wrapper/retry() around a
real API call for this scenario; every case here is a plain, zero-cost assertion.

Usage: .venv/bin/python fake_sandbox/test_scenario9_unexpected_plan.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario9-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.completion_log import read_logs_between  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.diff_write import encode_block_meta  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario9_unexpected_plan")


def _events_by_id(bridge, now):
    events = bridge.list_events(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=["Test Bucket"])
    return {e.identifier: e for e in events}


def case_a_cascade_shrinks_next_bucket():
    trace.step("Case A: 'unexpected_plan' on Test Gym deterministically shrinks Test Hobby's own front")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 14, 50)  # 20 min after Gym's own end — real elapsed time to reclaim

    gym_id = bridge.create_event("Test Gym", datetime(2026, 7, 23, 14, 0), datetime(2026, 7, 23, 14, 30), calendar_title="Test Bucket")
    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 23, 14, 30), datetime(2026, 7, 23, 17, 30), calendar_title="Test Bucket")
    by_id = _events_by_id(bridge, now)

    state = AgentState()
    trace.call("_resolve_external_block", triggering_task="Test Gym", answer="unexpected_plan", now=now.isoformat())
    orchestrator._resolve_external_block(
        bridge, rules, state, now, by_id[gym_id], "unexpected_plan", [by_id[hobby_id]],
        fixed_events=[], specific_events=[by_id[gym_id]], other_agent_blocks=[],
    )

    hobby_after = _events_by_id(bridge, now)[hobby_id]
    trace.check("Test Hobby's start moved to now (14:50), reclaiming the elapsed time", now, hobby_after.start)
    trace.check("Test Hobby's end is untouched (17:30)", datetime(2026, 7, 23, 17, 30), hobby_after.end)

    trace.check("Test Gym's id landed in resolved_block_ids", {gym_id}, state.resolved_block_ids)
    trace.check("pending_followup_reason is 'unexpected'", "unexpected", state.pending_followup_reason)
    trace.check(
        "pending_followup_at set per followup_delay_unexpected_minutes",
        now + timedelta(minutes=rules.followup_delay_unexpected_minutes), state.pending_followup_at,
    )

    entries = read_logs_between(now.date(), now.date())
    entry = next((e for e in entries if e["task"] == "Test Gym"), None)
    trace.check("completion log entry written for Test Gym", True, entry is not None)
    if entry is not None:
        trace.check("logged source is 'external'", "external", entry["source"])
        trace.check("logged status is 'bumped' (unexpected_plan)", "bumped", entry["status"])


def case_b_no_upcoming_bucket():
    trace.step("Case B: no upcoming bucket -> no adjustment attempted, followup still set")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 25, 14, 35)  # own date — avoids sharing a log file with other cases

    gym_id = bridge.create_event("Test Gym", datetime(2026, 7, 25, 14, 0), datetime(2026, 7, 25, 14, 30), calendar_title="Test Bucket")
    by_id = _events_by_id(bridge, now)

    state = AgentState()
    trace.call("_resolve_external_block", triggering_task="Test Gym", answer="unexpected_plan", bucket_blocks=[], now=now.isoformat())
    orchestrator._resolve_external_block(
        bridge, rules, state, now, by_id[gym_id], "unexpected_plan", [], fixed_events=[], specific_events=[by_id[gym_id]],
        other_agent_blocks=[],
    )

    trace.check("Test Gym's id still landed in resolved_block_ids", {gym_id}, state.resolved_block_ids)
    trace.check("pending_followup_reason still set to 'unexpected'", "unexpected", state.pending_followup_reason)


def case_c_completed_no_followup():
    trace.step("Case C: answer='completed' -> no followup, no cascade attempt")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 24, 14, 35)  # own date, same reason as Case B

    gym_id = bridge.create_event("Test Gym", datetime(2026, 7, 24, 14, 0), datetime(2026, 7, 24, 14, 30), calendar_title="Test Bucket")
    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 24, 14, 30), datetime(2026, 7, 24, 17, 30), calendar_title="Test Bucket")
    by_id = _events_by_id(bridge, now)

    state = AgentState()
    trace.call("_resolve_external_block", triggering_task="Test Gym", answer="completed", now=now.isoformat())
    orchestrator._resolve_external_block(
        bridge, rules, state, now, by_id[gym_id], "completed", [by_id[hobby_id]],
        fixed_events=[], specific_events=[by_id[gym_id]], other_agent_blocks=[],
    )

    hobby_after = _events_by_id(bridge, now)[hobby_id]
    trace.check("Test Hobby is untouched on a 'completed' answer", datetime(2026, 7, 24, 14, 30), hobby_after.start)
    trace.check("no followup set for a 'completed' answer", None, state.pending_followup_reason)
    trace.check("pending_followup_at is None", None, state.pending_followup_at)

    entries = read_logs_between(now.date(), now.date())
    entry = next((e for e in entries if e["task"] == "Test Gym"), None)
    trace.check("completion log entry written for Test Gym", True, entry is not None)
    if entry is not None:
        trace.check("logged status is 'completed'", "completed", entry["status"])


def case_d_bucket_with_existing_content_is_skipped():
    trace.step("Case D: next bucket already has agent-authored content -> skipped, left untouched")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 22, 14, 50)  # own date, avoids collisions with the other cases

    gym_id = bridge.create_event("Test Gym", datetime(2026, 7, 22, 14, 0), datetime(2026, 7, 22, 14, 30), calendar_title="Test Bucket")
    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 22, 14, 30), datetime(2026, 7, 22, 17, 30), calendar_title="Test Bucket")
    meta_notes = encode_block_meta(
        ProposedBlock(bucket_event_id=hobby_id, title="Existing task", start=datetime(2026, 7, 22, 14, 30),
                      end=datetime(2026, 7, 22, 15, 0), source="reminder", source_id="some-reminder-id")
    )
    bridge.create_event(
        "Existing task", datetime(2026, 7, 22, 14, 30), datetime(2026, 7, 22, 15, 0),
        calendar_title=rules.agent_calendar, notes=meta_notes,
    )
    by_id = _events_by_id(bridge, now)
    existing_agent_block = bridge.list_events(
        now.replace(hour=0, minute=0), now.replace(hour=23, minute=59), calendar_titles=[rules.agent_calendar],
    )

    state = AgentState()
    trace.call(
        "_resolve_external_block", triggering_task="Test Gym", answer="unexpected_plan", now=now.isoformat(),
        other_agent_blocks="[Existing task already in Test Hobby]",
    )
    orchestrator._resolve_external_block(
        bridge, rules, state, now, by_id[gym_id], "unexpected_plan", [by_id[hobby_id]],
        fixed_events=[], specific_events=[by_id[gym_id]], other_agent_blocks=existing_agent_block,
    )

    hobby_after = _events_by_id(bridge, now)[hobby_id]
    trace.check(
        "Test Hobby's own boundary is untouched — already has content, deliberately not shrunk",
        datetime(2026, 7, 22, 14, 30), hobby_after.start,
    )


def case_e_elapsed_time_leaves_too_little_room():
    trace.step("Case E: elapsed time already consumes the whole next bucket -> no adjustment, no crash")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    # min_block_minutes is 10 in rules.test.yaml — Hobby is 14:30-14:38, only 8 real
    # minutes total, already below the floor regardless of when `now` lands.
    now = datetime(2026, 7, 21, 14, 32)

    gym_id = bridge.create_event("Test Gym", datetime(2026, 7, 21, 14, 0), datetime(2026, 7, 21, 14, 30), calendar_title="Test Bucket")
    hobby_id = bridge.create_event("Test Hobby", datetime(2026, 7, 21, 14, 30), datetime(2026, 7, 21, 14, 38), calendar_title="Test Bucket")
    by_id = _events_by_id(bridge, now)

    state = AgentState()
    trace.call("_resolve_external_block", triggering_task="Test Gym", answer="unexpected_plan", now=now.isoformat())
    orchestrator._resolve_external_block(
        bridge, rules, state, now, by_id[gym_id], "unexpected_plan", [by_id[hobby_id]],
        fixed_events=[], specific_events=[by_id[gym_id]], other_agent_blocks=[],
    )

    hobby_after = _events_by_id(bridge, now)[hobby_id]
    trace.check(
        "Test Hobby's own boundary is untouched — below min_block_minutes either way",
        datetime(2026, 7, 21, 14, 30), hobby_after.start,
    )
    trace.check("pending_followup_reason still set (the check-in itself still resolves normally)", "unexpected", state.pending_followup_reason)


def main():
    ok = True
    cases = [
        ("Case A (cascade shrinks next bucket)", case_a_cascade_shrinks_next_bucket),
        ("Case B (no upcoming bucket)", case_b_no_upcoming_bucket),
        ("Case C (completed, no followup)", case_c_completed_no_followup),
        ("Case D (bucket with existing content is skipped)", case_d_bucket_with_existing_content_is_skipped),
        ("Case E (elapsed time leaves too little room)", case_e_elapsed_time_leaves_too_little_room),
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
