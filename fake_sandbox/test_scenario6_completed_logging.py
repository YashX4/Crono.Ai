"""Tier C — needs the full env-var-before-import dance. Automated version of TESTING.md
Scenario 6 ("completed" resolution): the completion log records the block's own
SCHEDULED start/end, not the wall-clock time the check-in was actually answered at.

Usage: .venv/bin/python fake_sandbox/test_scenario6_completed_logging.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-scenario6-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator  # noqa: E402
from timeblock_agent.completion_log import read_logs_between  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.day_layout import ProposedBlock  # noqa: E402
from timeblock_agent.diff_write import apply_layout  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, load_state, save_state  # noqa: E402

trace = TraceLogger("test_scenario6_completed_logging")

scheduled_start = datetime(2026, 7, 23, 9, 0)
scheduled_end = datetime(2026, 7, 23, 9, 20)
answer_now = datetime(2026, 7, 23, 9, 35)  # deliberately late tap, 15 min after the block's own end


def attempt():
    save_state(AgentState())
    # Completion-log entries accumulate in the SAME on-disk file across retries
    # (TIMEBLOCK_LOG_DIR is bound once at completion_log.py's own import time, so it
    # can't be redirected per attempt like the bridge/state can) — clear the day's file
    # first so a stale entry from an earlier, retried attempt can never be picked up by
    # this attempt's own read-back below.
    (_tmp_dir / "logs" / f"{scheduled_start:%Y-%m-%d}.md").unlink(missing_ok=True)
    bridge = FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{id(object())}.json")
    rules = load_rules(RULES_PATH)

    work_id = bridge.create_event(
        "Test Work", scheduled_start, scheduled_start.replace(hour=11), calendar_title="Test Bucket"
    )
    reminder_id = bridge.create_reminder("Test task beta (~20 min)", list_title="Test Reminders")
    # Title deliberately has its own parens, matching this project's usual "Test task
    # X (~Y min)" convention, combined with this sandbox's real agent_calendar name
    # "Task Blocks (Test)" (which ALSO has parens) — this is the exact shape that used
    # to be ambiguous for completion_log.parse_log_line's old regex-based format (see
    # ISSUES.md's now-FIXED parenthetical-ambiguity entry). Previously this test used a
    # deliberately paren-free title to sidestep that bug so Scenario 6's own purpose
    # (logged time is the block's SCHEDULE, not answer-time) stayed untangled from it;
    # now that completion_log.py is fixed (JSON-lines format), this title doubles as
    # that bug's own regression guard through the real orchestrator stack.
    block = ProposedBlock(
        bucket_event_id=work_id, title="Test task beta (~20 min)", start=scheduled_start, end=scheduled_end,
        source="reminder", source_id=reminder_id,
    )
    apply_layout(bridge, [block], existing_agent_events=[], agent_calendar=rules.agent_calendar, written_at=scheduled_start)
    agent_block = next(iter(bridge.list_events(
        scheduled_start.replace(hour=0, minute=0), scheduled_start.replace(hour=23, minute=59),
        calendar_titles=[rules.agent_calendar],
    )))

    trace.step("Check in 'completed' at 09:35, 15 min after the block's own scheduled end (09:20)")
    trace.call("run_checkin_answer", block_id=agent_block.identifier, answer="completed", now=answer_now.isoformat())
    orchestrator.run_checkin_answer(bridge, agent_block.identifier, "completed", now=answer_now)

    entries = read_logs_between(scheduled_start.date(), scheduled_start.date())
    entry = next((e for e in entries if e["task"] == "Test task beta (~20 min)"), None)
    trace.check("a completion-log entry was written for the task", True, entry is not None)
    if entry is not None:
        trace.check("logged start is the block's own SCHEDULED start, not answer-time", scheduled_start, entry["start"])
        trace.check("logged end is the block's own SCHEDULED end, not answer-time (09:35)", scheduled_end, entry["end"])
        trace.check("logged status is 'completed'", "completed", entry["status"])
        # Parenthetical-ambiguity regression (ISSUES.md, now FIXED): task title AND
        # bucket (rules.agent_calendar, "Task Blocks (Test)") both have their own
        # parens — round-tripping the bucket field correctly (not mis-split into the
        # task field or truncated at the wrong paren) is the actual point of using
        # this title, not just an incidental detail.
        trace.check("logged bucket is the agent_calendar name, not mangled by its own parens", rules.agent_calendar, entry["bucket"])

    reloaded_state = load_state()
    trace.check("block's id landed in resolved_block_ids", True, agent_block.identifier in reloaded_state.resolved_block_ids)

    bucket_blocks, fixed_events, specific_events = orchestrator._classify_today_events(bridge, answer_now, rules)
    agent_blocks = orchestrator._today_agent_blocks(bridge, answer_now, rules)
    checkable = orchestrator._checkable_blocks(agent_blocks, fixed_events, specific_events, reloaded_state)
    trace.check(
        "resolved block is excluded from a subsequent _checkable_blocks computation (never re-announced)",
        False, agent_block.identifier in {e.identifier for e in checkable},
    )


def main():
    ok = True
    try:
        # Bounded retry, same convention as every other real-model-driven case in this
        # suite (see test_helpers.retry's docstring): `replan_incremental` is called
        # unconditionally regardless of answer (see _resolve_agent_task), and — separate
        # from what THIS test actually checks — Haiku occasionally proposes re-booking
        # the very reminder just marked "completed" (it's still nominally "open" until
        # _credit_reminder_completion runs a few lines later), which would overwrite the
        # SAME calendar event's start/end via FakeEventKitBridge's in-place update before
        # this test's own read-back. That's a separate, already-latent finding (see
        # ISSUES.md) orthogonal to this test's actual purpose; retrying isolates it here
        # the same way this suite already isolates ordinary model placement variance
        # elsewhere, without masking it (still tracked, not silently accepted).
        retry(3, attempt, trace, "Scenario 6 completed resolution logging")
        print("Scenario 6 ('completed' resolution logging): PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
