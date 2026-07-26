"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 10 (day-end confirmation). Empty bridge throughout — 0 real API calls (an empty
`bucket_blocks`/reminders list short-circuits classify_events for free).

Note: handle_day_boundary/run_scheduled_tick load/save AgentState from disk internally
(no way to hand them a state object directly) — every case here seeds starting state via
save_state() first, calls the real function, then load_state() to inspect the outcome.

Usage: .venv/bin/python fake_sandbox/test_scenario10_day_end.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario10-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, load_state, save_state  # noqa: E402

trace = TraceLogger("test_scenario10_day_end")


def case_a_day_end_yes():
    trace.step("Case A: day_end 'yes' confirms the day, morning_floor_retry wins the trigger race")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_a.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 20, 0)

    trace.call("handle_day_boundary", event="day_end", answer="yes", now=now.isoformat())
    trigger = orchestrator.handle_day_boundary(bridge, "day_end", "yes", now=now)

    state = load_state()
    trace.check("day_end_confirmed_date set to today", now.date(), state.day_end_confirmed_date)
    trace.check("trigger is morning_floor_retry (day_start not yet confirmed today)", "morning_floor_retry", trigger.reason)
    trace.check(
        "trigger fires at now + floor_confirmation_retry_minutes",
        now + timedelta(minutes=rules.floor_confirmation_retry_minutes), trigger.at,
    )


def case_b_weekly_review_wins():
    trace.step("Case B: both floors already confirmed today -> weekly_review wins the trigger race")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 20, 0)
    last_review = now - timedelta(hours=1)
    save_state(AgentState(day_start_confirmed_date=now.date(), day_end_confirmed_date=now.date(), last_weekly_review_at=last_review))
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_b.json")  # empty -> classify_events short-circuits free

    trace.call("handle_day_boundary", event="day_end", answer="no", now=now.isoformat())
    trigger = orchestrator.handle_day_boundary(bridge, "day_end", "no", now=now)

    trace.check("trigger is weekly_review", "weekly_review", trigger.reason)
    trace.check(
        "trigger fires at last_weekly_review_at + weekly_review_interval_days",
        last_review + timedelta(days=rules.weekly_review_interval_days), trigger.at,
    )


def case_c_day_end_no_is_noop():
    trace.step("Case C: day_end 'no' leaves day_end_confirmed_date unchanged")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_c.json")
    now = datetime(2026, 7, 23, 20, 0)

    trace.call("handle_day_boundary", event="day_end", answer="no", now=now.isoformat())
    orchestrator.handle_day_boundary(bridge, "day_end", "no", now=now)

    state = load_state()
    trace.check("day_end_confirmed_date stays None (unchanged)", None, state.day_end_confirmed_date)


def case_d_e_evening_floor_retry_cadence():
    trace.step("Case D/E: run_scheduled_tick past evening_checkin_floor re-fires the notification on retry")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 23, 59, 30)  # 30s past evening_checkin_floor (23:59:00)
    save_state(AgentState(day_start_confirmed_date=now.date(), day_end_confirmed_date=None))
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_de.json")

    wrapper, calls = make_capturing_wrapper(orchestrator.send_notification)
    with mock.patch("timeblock_agent.orchestrator.send_notification", side_effect=wrapper):
        trace.call("run_scheduled_tick", now=now.isoformat())
        trigger1 = orchestrator.run_scheduled_tick(bridge, now=now)

    day_boundary_end_calls = [c for c in calls if c["args"][0] == "day_boundary_end"]
    trace.check("day_boundary_end notification fired on first tick", 1, len(day_boundary_end_calls))
    trace.check("trigger is evening_floor_retry", "evening_floor_retry", trigger1.reason)
    trace.check(
        "retry fires at now + floor_confirmation_retry_minutes", now + timedelta(minutes=rules.floor_confirmation_retry_minutes), trigger1.at,
    )

    # Deliberately NOT `trigger1.at` (now + floor_confirmation_retry_minutes) — with
    # rules.test.yaml's evening_checkin_floor at 23:59 and now already in the day's last
    # minute, adding the retry interval always rolls into the NEXT calendar day, resetting
    # the "past evening floor" condition entirely for that (brand new) day — a genuine
    # test-setup incompatibility with this specific near-midnight config value, not a
    # product bug. A smaller same-day gap still directly tests the actual property this
    # case cares about: does a second, still-unconfirmed tick re-ask, or suppress once
    # already asked?
    now2 = now + timedelta(seconds=10)
    wrapper2, calls2 = make_capturing_wrapper(orchestrator.send_notification)
    with mock.patch("timeblock_agent.orchestrator.send_notification", side_effect=wrapper2):
        trace.call("run_scheduled_tick (retry, same day)", now=now2.isoformat())
        orchestrator.run_scheduled_tick(bridge, now=now2)

    day_boundary_end_calls2 = [c for c in calls2 if c["args"][0] == "day_boundary_end"]
    trace.check("day_boundary_end notification fires AGAIN on the retry tick (re-ask, not one-shot)", 1, len(day_boundary_end_calls2))


def case_f_confirmed_day_end_suppresses_an_overdue_checkin():
    trace.step(
        "Case F: day_end already confirmed -> a still-overdue FIXED block does NOT get a "
        "check-in (regression guard: caught live, TESTING_LOG.md Session 5 — a real "
        "meeting's overdue check-in fired 7 seconds after 'check-ins are done until "
        "tomorrow' was sent, since this whole branch used to run unconditionally)"
    )
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 22, 3)
    save_state(AgentState(day_start_confirmed_date=now.date(), day_end_confirmed_date=now.date()))
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_f.json")
    # "meeting" matches always_fixed_hints (rules.test.yaml) — forced FIXED in code, zero
    # API calls, matching this whole file's "0 real API calls" design.
    bridge.create_event("Test Meeting", datetime(2026, 7, 23, 22, 0), datetime(2026, 7, 23, 22, 3), calendar_title="Test Bucket")

    wrapper, calls = make_capturing_wrapper(orchestrator.send_notification)
    with mock.patch("timeblock_agent.orchestrator.send_notification", side_effect=wrapper):
        trace.call("run_scheduled_tick", now=now.isoformat())
        trigger = orchestrator.run_scheduled_tick(bridge, now=now)

    checkin_calls = [c for c in calls if c["args"][0] == "checkin"]
    trace.check("no 'checkin' notification fired for the overdue meeting", 0, len(checkin_calls))
    day_boundary_end_calls = [c for c in calls if c["args"][0] == "day_boundary_end"]
    trace.check("no re-ask either — day_end is already confirmed", 0, len(day_boundary_end_calls))
    trace.check(
        "trigger does NOT track the meeting's own end anymore",
        True, not (trigger.reason == "block_end" and trigger.at == datetime(2026, 7, 23, 22, 3)),
    )


def main():
    ok = True
    cases = [
        ("Case A (day_end yes)", case_a_day_end_yes),
        ("Case B (weekly_review wins)", case_b_weekly_review_wins),
        ("Case C (day_end no is no-op)", case_c_day_end_no_is_noop),
        ("Case D/E (evening floor retry cadence)", case_d_e_evening_floor_retry_cadence),
        ("Case F (confirmed day_end suppresses an overdue check-in)", case_f_confirmed_day_end_suppresses_an_overdue_checkin),
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
