"""Tier B — real Claude API call, no bridge, no env setup (incremental_replan.py never
touches disk or reads a TIMEBLOCK_*_PATH). Drives incremental_replan.replan_incremental
directly for rule 4's two priority-judgment branches — PUSH (next bucket judged
equal-or-higher priority) and SHRINK-FROM-FRONT (next bucket judged lower priority) —
each end-to-end through a real model call, not just the isolated shape-validation logic.

Bounded 3-attempt retry per case given known model non-determinism (see
TESTING_LOG.md's Scenario 7/8 manual runs, which needed 1-3 attempts to land in a
specific condition).

Usage: .venv/bin/python fake_sandbox/test_priority_cascade_end_to_end.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, load_test_bucket_context, load_test_rules, make_event, make_reminder, retry  # noqa: E402
from timeblock_agent.incremental_replan import replan_incremental  # noqa: E402

trace = TraceLogger("test_priority_cascade_end_to_end")


def _find_adjustment(result, bucket_id):
    return next((a for a in result.bucket_adjustments if a.bucket_event_id == bucket_id), None)


def branch_b_shrink_from_front():
    trace.step("Branch B: Test Work overruns into Test Hobby (LOWER priority) -> SHRINK-FROM-FRONT")
    rules = load_test_rules()
    bucket_context = load_test_bucket_context()
    now = datetime(2026, 7, 23, 10, 0)

    def attempt():
        # Filled exactly, zero natural slack, so the cascade is forced immediately.
        work = make_event("work1", "Test Work", datetime(2026, 7, 23, 9, 0), now)
        hobby = make_event("hobby1", "Test Hobby", now, datetime(2026, 7, 23, 11, 0))
        beta = make_reminder("beta_id", "Test task beta (~20 min)")

        trace.call(
            "replan_incremental", resolution="running_behind", triggering_block_id="work1",
            now=now.isoformat(), triggering_block_start="09:00", triggering_block_end="10:00",
        )
        result = replan_incremental(
            now, "running_behind", "work1", [work, hobby], [beta], rules,
            triggering_source="reminder", triggering_source_id="beta_id",
            bucket_context=bucket_context,
            triggering_block_start=datetime(2026, 7, 23, 9, 0), triggering_block_end=now,
        )

        work_adj = _find_adjustment(result, "work1")
        hobby_adj = _find_adjustment(result, "hobby1")
        trace.check("Test Work got a bucket_adjustment (EXTEND)", True, work_adj is not None)
        trace.check("Test Work's new_start unchanged", datetime(2026, 7, 23, 9, 0), work_adj.new_start)
        trace.check(
            "Test Work's new_end extended by followup_delay_continuing_minutes (3 min)",
            datetime(2026, 7, 23, 10, 3), work_adj.new_end,
        )
        trace.check("Test Hobby got a bucket_adjustment (SHRINK-FROM-FRONT)", True, hobby_adj is not None)
        trace.check("Test Hobby's new_end unchanged", datetime(2026, 7, 23, 11, 0), hobby_adj.new_end)
        trace.check(
            "Test Hobby's new_start pushed to match Work's new end (matching pair)",
            datetime(2026, 7, 23, 10, 3), hobby_adj.new_start,
        )

        continuation = next(
            (b for b in result.blocks if b.source_id == "beta_id" and b.start == datetime(2026, 7, 23, 9, 0)), None
        )
        trace.check("triggering continuation block present", True, continuation is not None)
        trace.check("triggering continuation block's new end", datetime(2026, 7, 23, 10, 3), continuation.end)

    retry(3, attempt, trace, "Branch B")


def branch_a_push():
    trace.step("Branch A: Test Hobby overruns into Test Work (HIGHER priority) -> PUSH")
    rules = load_test_rules()
    bucket_context = load_test_bucket_context()
    now = datetime(2026, 7, 23, 9, 20)

    def attempt():
        # Same two buckets, chronological order reversed — no new bucket shapes needed.
        hobby = make_event("hobby1", "Test Hobby", datetime(2026, 7, 23, 9, 0), now)
        work = make_event("work1", "Test Work", now, datetime(2026, 7, 23, 10, 20))
        gamma = make_reminder("gamma_id", "Test task gamma (~30 min)")

        trace.call(
            "replan_incremental", resolution="running_behind", triggering_block_id="hobby1",
            now=now.isoformat(), triggering_block_start="09:00", triggering_block_end="09:20",
        )
        result = replan_incremental(
            now, "running_behind", "hobby1", [hobby, work], [gamma], rules,
            triggering_source="reminder", triggering_source_id="gamma_id",
            bucket_context=bucket_context,
            triggering_block_start=datetime(2026, 7, 23, 9, 0), triggering_block_end=now,
        )

        hobby_adj = _find_adjustment(result, "hobby1")
        work_adj = _find_adjustment(result, "work1")
        trace.check("Test Hobby got a bucket_adjustment (EXTEND)", True, hobby_adj is not None)
        trace.check("Test Hobby's new_start unchanged", datetime(2026, 7, 23, 9, 0), hobby_adj.new_start)
        trace.check(
            "Test Hobby's new_end extended by followup_delay_continuing_minutes (3 min)",
            datetime(2026, 7, 23, 9, 23), hobby_adj.new_end,
        )
        trace.check("Test Work got a bucket_adjustment (PUSH)", True, work_adj is not None)
        trace.check("Test Work's new_start pushed later", datetime(2026, 7, 23, 9, 23), work_adj.new_start)
        trace.check(
            "Test Work's duration preserved at exactly 60 min (PUSH, not a wrongly-shrunk end)",
            timedelta(minutes=60), work_adj.new_end - work_adj.new_start,
        )
        trace.check("Test Work's new_end", datetime(2026, 7, 23, 10, 23), work_adj.new_end)

    retry(3, attempt, trace, "Branch A")


def main():
    ok = True
    try:
        branch_b_shrink_from_front()
        print("Branch B (SHRINK-FROM-FRONT): PASS")
        branch_a_push()
        print("Branch A (PUSH): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
