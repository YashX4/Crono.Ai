"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 11 (implicit day-boundary fallback) — the system being off/asleep past
`day_boundary_gap_hours` with no explicit day_start confirmed must auto-recover on the
next tick, without a real explicit "are you up?" round-trip. Empty bridge, 0 API calls.

Usage: .venv/bin/python fake_sandbox/test_scenario11_implicit_boundary.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario11-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, load_state, save_state  # noqa: E402

trace = TraceLogger("test_scenario11_implicit_boundary")


def case_a_implicit_boundary_force_confirms():
    trace.step("Case A: gap beyond day_boundary_gap_hours -> run_scheduled_tick force-confirms day_start")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 9, 0)
    yesterday = (now - timedelta(days=1)).date()
    gap_hours = rules.day_boundary_gap_hours + 1  # comfortably over the ~3-min threshold

    save_state(AgentState(
        last_run_at=now - timedelta(hours=gap_hours),
        day_start_confirmed_date=yesterday,
        day_end_confirmed_date=yesterday,
        resolved_block_ids={"stale1"},
        started_block_ids={"stale2"},
    ))
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_a.json")  # empty -> classify_events free

    trace.call("run_scheduled_tick", now=now.isoformat())
    trigger = orchestrator.run_scheduled_tick(bridge, now=now)

    state = load_state()
    trace.check("day_start_confirmed_date advances to today", now.date(), state.day_start_confirmed_date)
    trace.check("day_end_confirmed_date explicitly reset to None", None, state.day_end_confirmed_date)
    trace.check("resolved_block_ids cleared (proves _confirm_day_start actually ran)", set(), state.resolved_block_ids)
    trace.check("started_block_ids cleared (proves _confirm_day_start actually ran)", set(), state.started_block_ids)
    trace.check("last_run_at updated to now", now, state.last_run_at)
    trace.check("last_weekly_review_at seeded to now (first-ever confirm)", now, state.last_weekly_review_at)
    trace.check("trigger is evening_floor (both other candidates suppressed/far)", "evening_floor", trigger.reason)


def case_b_no_implicit_boundary_when_gap_is_small():
    trace.step("Case B (negative control): gap under threshold -> the branch correctly does NOT fire")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 9, 0)

    save_state(AgentState(
        last_run_at=now - timedelta(minutes=1),  # well under the ~3-min day_boundary_gap_hours threshold
        day_start_confirmed_date=now.date(),
        day_end_confirmed_date=None,
        resolved_block_ids={"kept"},
        started_block_ids={"kept2"},
    ))
    bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit_b.json")

    trace.call("run_scheduled_tick", now=now.isoformat())
    orchestrator.run_scheduled_tick(bridge, now=now)

    state = load_state()
    trace.check("resolved_block_ids unchanged (proves _confirm_day_start was NOT invoked)", {"kept"}, state.resolved_block_ids)
    trace.check("started_block_ids unchanged (proves _confirm_day_start was NOT invoked)", {"kept2"}, state.started_block_ids)
    trace.check("day_start_confirmed_date still today (unchanged, already was)", now.date(), state.day_start_confirmed_date)


def main():
    ok = True
    cases = [
        ("Case A (implicit boundary force-confirms)", case_a_implicit_boundary_force_confirms),
        ("Case B (negative control, no implicit boundary)", case_b_no_implicit_boundary_when_gap_is_small),
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
