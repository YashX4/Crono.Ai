"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 5 (start/end check-ins fire at the right scope): FIXED/FLEXIBLE_SPECIFIC blocks
get checked in on like agent tasks, but bucket CONTAINERS themselves never do.

Usage: .venv/bin/python fake_sandbox/test_scenario5_checkin_scope.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario5-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.diff_write import apply_layout  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_scenario5_checkin_scope")


def checkable_blocks_case():
    trace.step("Case 1: _checkable_blocks includes task blocks + FIXED/SPECIFIC, never bucket containers")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)
    now = datetime(2026, 7, 23, 9, 0)

    work_id = bridge.create_event(
        "Test Work", now.replace(hour=9, minute=0), now.replace(hour=11, minute=0), calendar_title="Test Bucket"
    )
    meeting_id = bridge.create_event(
        "Test Meeting", now.replace(hour=10, minute=0), now.replace(hour=10, minute=15), calendar_title="Test Bucket"
    )

    block = ProposedBlock(
        bucket_event_id=work_id, title="Some task", start=now.replace(hour=9, minute=0), end=now.replace(hour=9, minute=20),
        source="reminder", source_id="rem1",
    )
    apply_layout(bridge, [block], existing_agent_events=[], agent_calendar=rules.agent_calendar, written_at=now)

    bucket_blocks, fixed_events, specific_events = orchestrator._classify_today_events(bridge, now, rules)
    agent_blocks = orchestrator._today_agent_blocks(bridge, now, rules)
    checkable = orchestrator._checkable_blocks(agent_blocks, fixed_events, specific_events, AgentState())
    checkable_ids = {e.identifier for e in checkable}

    trace.check("Test Work's own bucket-container id is NOT checkable", False, work_id in checkable_ids)
    trace.check("the agent task block IS checkable", True, any(e.title == "Some task" for e in checkable))
    trace.check("Test Meeting (FIXED) IS checkable", True, meeting_id in checkable_ids)


def checkin_routing_case():
    trace.step("Case 2: checking in on a FIXED block routes to _resolve_external_block, never _resolve_agent_task")
    save_state(AgentState())
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    now = datetime(2026, 7, 23, 10, 20)

    bridge.create_event("Test Work", now.replace(hour=9, minute=0), now.replace(hour=11, minute=0), calendar_title="Test Bucket")
    meeting_id = bridge.create_event(
        "Test Meeting", now.replace(hour=10, minute=0), now.replace(hour=10, minute=15), calendar_title="Test Bucket"
    )

    ext_wrapper, ext_calls = make_capturing_wrapper(orchestrator._resolve_external_block)
    agent_wrapper, agent_calls = make_capturing_wrapper(orchestrator._resolve_agent_task)
    with mock.patch("timeblock_agent.orchestrator._resolve_external_block", side_effect=ext_wrapper), \
         mock.patch("timeblock_agent.orchestrator._resolve_agent_task", side_effect=agent_wrapper):
        trace.call("run_checkin_answer", block_id=meeting_id, answer="completed", now=now.isoformat())
        orchestrator.run_checkin_answer(bridge, meeting_id, "completed", now=now)

    trace.check("_resolve_external_block was called exactly once", 1, len(ext_calls))
    trace.check("_resolve_agent_task was never called", 0, len(agent_calls))


def main():
    ok = True
    cases = [
        ("Case 1 (_checkable_blocks scoping)", checkable_blocks_case),
        ("Case 2 (check-in routing)", checkin_routing_case),
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
