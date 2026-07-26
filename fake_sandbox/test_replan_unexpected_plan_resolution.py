"""Tier B — real Claude API call (core case only), no bridge, no env setup. Drives
incremental_replan.replan_incremental's "unexpected_plan" resolution directly — this
resolution has ZERO coverage anywhere else in the codebase (every existing test, mocked
or fake-sandbox, only ever exercises "running_behind"; production only reaches
"unexpected_plan" via orchestrator._resolve_external_block, itself covered separately by
test_scenario9_unexpected_plan.py, which spies on this same function rather than
duplicating its logic here).

Usage: .venv/bin/python fake_sandbox/test_replan_unexpected_plan_resolution.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, load_test_bucket_context, load_test_rules, make_event, make_reminder, retry  # noqa: E402
from timeblock_agent.incremental_replan import replan_incremental  # noqa: E402

trace = TraceLogger("test_replan_unexpected_plan_resolution")


def case_invalid_resolution_raises():
    trace.step("Case 1: invalid resolution string raises ValueError (free, no API call)")
    rules = load_test_rules()
    now = datetime(2026, 7, 23, 9, 0)
    work = make_event("work1", "Test Work", now, datetime(2026, 7, 23, 10, 0))
    raised = False
    try:
        replan_incremental(now, "bogus_resolution", "work1", [work], [], rules)
    except ValueError:
        raised = True
    trace.check("ValueError raised for an invalid resolution string", True, raised)


def case_empty_bucket_blocks_short_circuits():
    trace.step("Case 2: empty bucket_blocks short-circuits to an empty IncrementalResult (free)")
    rules = load_test_rules()
    now = datetime(2026, 7, 23, 9, 0)
    result = replan_incremental(now, "unexpected_plan", "work1", [], [], rules)
    trace.check("no blocks proposed", [], result.blocks)
    trace.check("no bucket_adjustments proposed", [], result.bucket_adjustments)
    trace.check("empty unscheduled_reminder_ids (no reminders were given)", [], result.unscheduled_reminder_ids)


def case_core_reminder_refills_reopened_bucket():
    trace.step("Case 3: 'unexpected_plan' reopens a bucket's time for refilling from reminders")
    rules = load_test_rules()
    bucket_context = load_test_bucket_context()
    now = datetime(2026, 7, 23, 9, 0)

    def attempt():
        bucket = make_event("hobby1", "Test Hobby", now, datetime(2026, 7, 23, 9, 30))
        quick = make_reminder("quick_id", "Test task quick (~15 min)")

        trace.call(
            "replan_incremental", resolution="unexpected_plan", triggering_block_id="hobby1", now=now.isoformat()
        )
        result = replan_incremental(
            now, "unexpected_plan", "hobby1", [bucket], [quick], rules, bucket_context=bucket_context,
        )
        placed = next((b for b in result.blocks if b.source == "reminder" and b.source_id == "quick_id"), None)
        trace.check("reminder got a chance to refill the reopened bucket", True, placed is not None)
        if placed is not None:
            trace.check("placed block starts within the bucket's window", True, placed.start >= bucket.start)
            trace.check("placed block ends within the bucket's window", True, placed.end <= bucket.end)

    retry(3, attempt, trace, "Core refill case")


def main():
    ok = True
    cases = [
        ("Case 1 (invalid resolution raises ValueError)", case_invalid_resolution_raises),
        ("Case 2 (empty bucket_blocks short-circuits)", case_empty_bucket_blocks_short_circuits),
        ("Case 3 (reminder refills reopened bucket)", case_core_reminder_refills_reopened_bucket),
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
