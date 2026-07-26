"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 13 (weekly review): completion-log stats + manual-edit detection turn into a
preferences.md observation, and the interval check correctly suppresses an immediate
re-fire right after a review just completed.

Usage: .venv/bin/python fake_sandbox/test_scenario13_weekly_review.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario13-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import completion_log, orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.diff_write import apply_layout  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.preferences import load_preferences  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario13_weekly_review")


def main():
    ok = True
    try:
        rules = load_rules(RULES_PATH)
        bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit.json")
        now = datetime(2026, 7, 23, 20, 0)
        since = now - timedelta(days=1)

        trace.step("Seed a couple resolved completion-log entries (bucket-stat material)")
        completion_log.log_block(
            datetime(2026, 7, 22, 9, 0), datetime(2026, 7, 22, 9, 20), "Write project update", "Task Blocks (Test)",
            "completed", "reminder",
        )
        completion_log.log_block(
            datetime(2026, 7, 22, 10, 0), datetime(2026, 7, 22, 10, 30), "Another task", "Task Blocks (Test)",
            "bumped", "reminder",
        )

        trace.step("Place one agent-authored block, then simulate a manual Calendar.app edit on it")
        work_id = bridge.create_event(
            "Test Work", datetime(2026, 7, 22, 9, 0), datetime(2026, 7, 22, 11, 0), calendar_title="Test Bucket"
        )
        block = ProposedBlock(
            bucket_event_id=work_id, title="Write project update", start=datetime(2026, 7, 22, 9, 0),
            end=datetime(2026, 7, 22, 9, 20), source="reminder", source_id="rem1",
        )
        apply_layout(bridge, [block], existing_agent_events=[], agent_calendar=rules.agent_calendar, written_at=since)
        agent_events = bridge.list_events(
            datetime(2026, 7, 22, 0, 0), datetime(2026, 7, 22, 23, 59), calendar_titles=[rules.agent_calendar]
        )
        placed = agent_events[0]
        # Manual edit, bypassing apply_layout entirely — not a system-initiated reshuffle.
        bridge.update_event(placed.identifier, title="Write project update (renamed by hand)")

        state = AgentState(last_weekly_review_at=since)
        # Spy on _detect_manual_edits rather than calling it again afterward — by the
        # time run_weekly_review returns, its own internal _purge_reviewed_snapshots(now)
        # call has already removed this (now-past-day) snapshot, so a separate call made
        # after the fact would always find nothing to compare against.
        wrapper, edit_calls = make_capturing_wrapper(orchestrator._detect_manual_edits)
        with mock.patch("timeblock_agent.orchestrator._detect_manual_edits", side_effect=wrapper):
            trace.call("run_weekly_review", now=now.isoformat())
            orchestrator.run_weekly_review(bridge, rules, state, now)

        trace.check("last_weekly_review_at advances to now", now, state.last_weekly_review_at)
        preferences_text = load_preferences()
        trace.check("preferences.md got a new Observations entry", True, bool(preferences_text.strip()))

        trace.check("_detect_manual_edits was called exactly once", 1, len(edit_calls))
        edits = edit_calls[0]["result"] if edit_calls else []
        trace.check("exactly one manual edit detected", 1, len(edits))
        if edits:
            trace.check("the detected edit is the retitled block", "retitled", edits[0].kind)

        trace.step("Immediately re-tick -> weekly review must NOT re-fire (interval, not retry cadence)")
        save_state(state)
        wrapper, calls = make_capturing_wrapper(orchestrator.run_weekly_review)
        with mock.patch("timeblock_agent.orchestrator.run_weekly_review", side_effect=wrapper):
            trace.call("run_scheduled_tick", now=now.isoformat())
            orchestrator.run_scheduled_tick(bridge, now=now)
        trace.check("run_weekly_review was NOT called again immediately after", 0, len(calls))

        print("Scenario 13 (weekly review): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
