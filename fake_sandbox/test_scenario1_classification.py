"""Tier B — real Claude API call for the ambiguous cases, no bridge, no env setup
(classify.py never touches disk or reads a TIMEBLOCK_*_PATH). Drives
classify.classify_events directly against the real buckets.test.md context, confirming
real-model classification CORRECTNESS — cache mechanics (partial hits, invalidation on a
bucket_context change) are already thoroughly covered by the existing mocked
test_classify_cache.py and deliberately not duplicated here.

Usage: .venv/bin/python fake_sandbox/test_scenario1_classification.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, load_test_bucket_context, load_test_rules, make_event, retry  # noqa: E402
from timeblock_agent.classify import classify_events, clear_classification_cache  # noqa: E402

trace = TraceLogger("test_scenario1_classification")


def case_always_fixed_hint_no_api_cost():
    trace.step("Case 1: 'Test Meeting' classified FIXED via the code-level always_fixed_hints pre-filter")
    rules = load_test_rules()
    meeting = make_event("meet1", "Test Meeting", datetime(2026, 7, 23, 10, 0), datetime(2026, 7, 23, 10, 15))
    result = classify_events([meeting], rules, "")  # empty bucket_context — pre-filter needs none of it
    trace.check("Test Meeting classified FIXED (pre-filter, zero API cost)", "FIXED", result["meet1"])


def case_real_model_classifications():
    trace.step("Case 2: real-model classification of Test Gym / Test Work / Test Hobby / Test Buffer")
    rules = load_test_rules()
    bucket_context = load_test_bucket_context()

    def attempt():
        clear_classification_cache()
        gym = make_event("gym1", "Test Gym", datetime(2026, 7, 23, 14, 0), datetime(2026, 7, 23, 14, 30))
        work = make_event("work1", "Test Work", datetime(2026, 7, 23, 9, 0), datetime(2026, 7, 23, 11, 0))
        hobby = make_event("hobby1", "Test Hobby", datetime(2026, 7, 23, 11, 0), datetime(2026, 7, 23, 14, 0))
        buffer_ = make_event("buffer1", "Test Buffer", datetime(2026, 7, 23, 14, 30), datetime(2026, 7, 23, 15, 0))

        trace.call("classify_events", events=["Test Gym", "Test Work", "Test Hobby", "Test Buffer"])
        result = classify_events([gym, work, hobby, buffer_], rules, bucket_context)

        trace.check("Test Gym classified FLEXIBLE_SPECIFIC", "FLEXIBLE_SPECIFIC", result["gym1"])
        trace.check("Test Work classified FLEXIBLE_BUCKET", "FLEXIBLE_BUCKET", result["work1"])
        trace.check("Test Hobby classified FLEXIBLE_BUCKET", "FLEXIBLE_BUCKET", result["hobby1"])
        trace.check("Test Buffer classified FLEXIBLE_BUCKET", "FLEXIBLE_BUCKET", result["buffer1"])

    retry(3, attempt, trace, "Real-model classifications")


def main():
    ok = True
    cases = [
        ("Case 1 (always_fixed_hints pre-filter)", case_always_fixed_hint_no_api_cost),
        ("Case 2 (real-model classifications)", case_real_model_classifications),
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
