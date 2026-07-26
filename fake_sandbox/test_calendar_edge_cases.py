"""Tier A — pure logic, zero I/O, zero API calls, zero env setup. Hand-built dataclasses
in, plain values out — see EDGE_CASES.md's tier legend. No setup_fake_env() needed here:
none of day_layout.py/goals.py/state.py's `is_implicit_day_boundary` touch disk or bind
a real-`~`-rooted path this file ever reads from.

Usage: .venv/bin/python fake_sandbox/test_calendar_edge_cases.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, load_test_rules, make_event  # noqa: E402
from timeblock_agent.config import GoalFillWeights  # noqa: E402
from timeblock_agent.day_layout import (  # noqa: E402
    BucketAdjustment,
    ProposedBlock,
    overlaps_fixed_event,
    validate_block,
    validate_bucket_adjustment,
)
from timeblock_agent.goals import Goal, allocate_goal_sessions  # noqa: E402
from timeblock_agent.state import AgentState, is_implicit_day_boundary  # noqa: E402

trace = TraceLogger("test_calendar_edge_cases")


def case1_all_day_event_blocks_everything():
    trace.step("Case 1: all-day FIXED event blocks everything that day")
    day = datetime(2026, 7, 23)
    allday = make_event("allday1", "Offsite", day, day + timedelta(days=1), is_all_day=True)
    work = make_event("work1", "Test Work", datetime(2026, 7, 23, 9, 0), datetime(2026, 7, 23, 17, 0))
    rules = load_test_rules()

    overlap = overlaps_fixed_event(datetime(2026, 7, 23, 9, 0), datetime(2026, 7, 23, 10, 0), [allday])
    trace.check("all-day event overlaps a normal daytime window", True, overlap)

    adj = BucketAdjustment(bucket_event_id="work1", new_start=work.start, new_end=datetime(2026, 7, 23, 18, 0))
    valid_adj = validate_bucket_adjustment(adj, {"work1": work}, datetime(2026, 7, 23, 23, 59), fixed_events=[allday])
    trace.check("bucket EXTEND rejected due to all-day fixed event", False, valid_adj)

    block = ProposedBlock(
        bucket_event_id="work1", title="x", start=datetime(2026, 7, 23, 9, 0), end=datetime(2026, 7, 23, 10, 0),
        source="reminder", source_id="r1",
    )
    valid_block = validate_block(block, {"work1": work}, rules, datetime(2026, 7, 23, 8, 0), fixed_events=[allday])
    trace.check("block rejected due to all-day fixed event", False, valid_block)


def case2_zero_gap_adjacency_allowed():
    trace.step("Case 2: zero-gap adjacency is allowed")
    fixed = make_event("meet1", "Test Meeting", datetime(2026, 7, 23, 10, 0), datetime(2026, 7, 23, 10, 15))
    work = make_event("work1", "Test Work", datetime(2026, 7, 23, 9, 0), datetime(2026, 7, 23, 17, 0))
    rules = load_test_rules()

    trace.check(
        "block ending exactly at fixed start doesn't overlap", False,
        overlaps_fixed_event(datetime(2026, 7, 23, 9, 45), datetime(2026, 7, 23, 10, 0), [fixed]),
    )
    trace.check(
        "block starting exactly at fixed end doesn't overlap", False,
        overlaps_fixed_event(datetime(2026, 7, 23, 10, 15), datetime(2026, 7, 23, 10, 30), [fixed]),
    )

    block = ProposedBlock(
        bucket_event_id="work1", title="x", start=datetime(2026, 7, 23, 10, 15), end=datetime(2026, 7, 23, 10, 35),
        source="reminder", source_id="r1",
    )
    trace.check(
        "validate_block accepts a block starting exactly at a fixed event's own end", True,
        validate_block(block, {"work1": work}, rules, datetime(2026, 7, 23, 9, 0), fixed_events=[fixed]),
    )


def case3_exact_hard_cutoff_boundary():
    trace.step("Case 3: exact hard_cutoff boundary — EXTEND and PUSH shapes")
    cutoff = datetime(2026, 7, 23, 23, 59)

    work = make_event("work1", "Test Work", datetime(2026, 7, 23, 20, 0), datetime(2026, 7, 23, 22, 0))
    adj_extend_exact = BucketAdjustment("work1", new_start=work.start, new_end=cutoff)
    trace.check(
        "EXTEND landing exactly at cutoff is accepted", True,
        validate_bucket_adjustment(adj_extend_exact, {"work1": work}, cutoff),
    )
    adj_extend_over = BucketAdjustment("work1", new_start=work.start, new_end=cutoff + timedelta(minutes=1))
    trace.check(
        "EXTEND landing 1 min past cutoff is rejected", False,
        validate_bucket_adjustment(adj_extend_over, {"work1": work}, cutoff),
    )

    hobby = make_event("hobby1", "Test Hobby", datetime(2026, 7, 23, 21, 0), datetime(2026, 7, 23, 22, 0))
    push_end = cutoff
    push_start = push_end - (hobby.end - hobby.start)
    adj_push_exact = BucketAdjustment("hobby1", new_start=push_start, new_end=push_end)
    trace.check(
        "PUSH landing exactly at cutoff is accepted", True,
        validate_bucket_adjustment(adj_push_exact, {"hobby1": hobby}, cutoff),
    )
    adj_push_over = BucketAdjustment(
        "hobby1", new_start=push_start + timedelta(minutes=1), new_end=push_end + timedelta(minutes=1)
    )
    trace.check(
        "PUSH landing 1 min past cutoff is rejected", False,
        validate_bucket_adjustment(adj_push_over, {"hobby1": hobby}, cutoff),
    )


def case4_goal_budget_exceeds_capacity():
    trace.step("Case 4: goal-budget exceeds total active-goal capacity")
    weights = GoalFillWeights(staleness=0.6, priority=0.4, max_minutes_per_goal=90)
    goal_a = Goal(
        path=Path("/tmp/goal_a.md"), title="Goal A", priority="high", status="active",
        last_touched=datetime(2020, 1, 1).date(), next_action="Do A",
    )
    goal_b = Goal(
        path=Path("/tmp/goal_b.md"), title="Goal B", priority="low", status="active",
        last_touched=datetime(2020, 6, 1).date(), next_action="Do B",
    )

    sessions = allocate_goal_sessions([goal_a, goal_b], weights, budget_minutes=500, max_minutes_per_goal=90)
    trace.check("exactly 2 sessions allocated (2 active goals)", 2, len(sessions))
    trace.check("total minutes allocated capped at 180, not 500 — documents current accepted behavior", 180, sum(s.minutes for s in sessions))
    trace.check("each session capped at exactly 90", True, all(s.minutes == 90 for s in sessions))

    trace.check("empty goals list returns no sessions", [], allocate_goal_sessions([], weights, 100, 90))
    trace.check("zero budget returns no sessions", [], allocate_goal_sessions([goal_a], weights, 0, 90))


def case5_implicit_day_boundary_threshold():
    trace.step("Case 5: is_implicit_day_boundary exact-threshold math")
    rules = load_test_rules()  # day_boundary_gap_hours = 0.05 (~3 min) in rules.test.yaml
    now = datetime(2026, 7, 23, 12, 0)

    state_exact = AgentState(last_run_at=now - timedelta(hours=rules.day_boundary_gap_hours))
    trace.check(
        "gap exactly equal to threshold is NOT an implicit boundary (strict >)", False,
        is_implicit_day_boundary(now, state_exact, rules),
    )
    state_over = AgentState(last_run_at=now - timedelta(hours=rules.day_boundary_gap_hours) - timedelta(seconds=1))
    trace.check(
        "gap one second over threshold IS an implicit boundary", True,
        is_implicit_day_boundary(now, state_over, rules),
    )


def case6_validate_block_rejects_start_before_now():
    trace.step(
        "Case 6: validate_block rejects a block starting before `now` (regression guard: "
        "caught live, TESTING_LOG.md Session 5 Scenario 11 — a delayed day-start pass "
        "placed a task block 14 minutes before the `now` it was explicitly given, with "
        "nothing in code to catch it; plan_day/replan_incremental's own prompts already "
        "said 'never before now' but neither had ever enforced it)"
    )
    rules = load_test_rules()
    work = make_event("work1", "Test Work", datetime(2026, 7, 23, 9, 0), datetime(2026, 7, 23, 17, 0))

    block = ProposedBlock(
        bucket_event_id="work1", title="x", start=datetime(2026, 7, 23, 10, 0), end=datetime(2026, 7, 23, 10, 20),
        source="reminder", source_id="r1",
    )
    trace.check(
        "block starting before now is rejected", False,
        validate_block(block, {"work1": work}, rules, datetime(2026, 7, 23, 10, 15)),
    )
    trace.check(
        "the SAME block is accepted once now catches up to (or is before) its own start", True,
        validate_block(block, {"work1": work}, rules, datetime(2026, 7, 23, 10, 0)),
    )
    trace.check(
        "a block starting exactly at now is accepted (not `> now`, `>= now`)", True,
        validate_block(
            ProposedBlock(bucket_event_id="work1", title="x", start=datetime(2026, 7, 23, 10, 0), end=datetime(2026, 7, 23, 10, 20), source="reminder", source_id="r1"),
            {"work1": work}, rules, datetime(2026, 7, 23, 10, 0),
        ),
    )


def main():
    ok = True
    cases = [
        ("Case 1 (all-day event blocks everything)", case1_all_day_event_blocks_everything),
        ("Case 2 (zero-gap adjacency allowed)", case2_zero_gap_adjacency_allowed),
        ("Case 3 (exact hard_cutoff boundary)", case3_exact_hard_cutoff_boundary),
        ("Case 4 (goal-budget exceeds capacity)", case4_goal_budget_exceeds_capacity),
        ("Case 5 (is_implicit_day_boundary threshold)", case5_implicit_day_boundary_threshold),
        ("Case 6 (validate_block rejects start before now)", case6_validate_block_rejects_start_before_now),
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
