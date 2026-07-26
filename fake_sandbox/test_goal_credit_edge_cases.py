"""Tier C — needs the full env-var-before-import dance. Calls
orchestrator._resolve_agent_task directly with a goal-sourced meta but zero goals on
disk (goal_candidate is None deterministically) — locks in "no candidate -> silent
no-credit, no crash" as documented intended behavior, not a silent surprise. Zero real
API calls: bucket_blocks=[] makes replan_incremental short-circuit for free.

Usage: .venv/bin/python fake_sandbox/test_goal_credit_edge_cases.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, make_event, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-goal-credit-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402

trace = TraceLogger("test_goal_credit_edge_cases")


def main():
    ok = True
    try:
        save_state(AgentState())
        bridge = FakeEventKitBridge(store_path=_tmp_dir / "eventkit.json")
        rules = load_rules(RULES_PATH)
        now = datetime(2026, 7, 23, 12, 0)

        triggering_task = make_event(
            "goal_block1", "Do some goal's next action", datetime(2026, 7, 23, 11, 0), datetime(2026, 7, 23, 12, 0),
            calendar_title=rules.agent_calendar,
        )
        meta = {"bucket_event_id": "work1", "source": "goal", "source_id": "goal"}
        state = AgentState()

        wrapper, calls = make_capturing_wrapper(orchestrator.record_completion)
        with mock.patch("timeblock_agent.orchestrator.record_completion", side_effect=wrapper):
            trace.call(
                "_resolve_agent_task", triggering_task=triggering_task.title, meta=meta, answer="completed", now=now.isoformat()
            )
            orchestrator._resolve_agent_task(
                bridge, rules, state, now, triggering_task, meta, "completed", [], [], specific_events=[],
            )

        trace.check("record_completion was never called (no goal candidate available)", 0, len(calls))
        trace.check("triggering task's id still landed in resolved_block_ids", {"goal_block1"}, state.resolved_block_ids)
        trace.check("pending_followup_at is None ('completed' clears any pending followup)", None, state.pending_followup_at)
        trace.check("pending_followup_reason is None", None, state.pending_followup_reason)

        print("Goal-credit-with-no-candidate: PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
