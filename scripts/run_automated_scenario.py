"""Automates sandbox scenarios end to end WITHOUT needing Telegram at all — Telegram is
just a thin UI layer over handle_day_boundary()/handle_goal_time_answer()/
run_checkin_answer(), so this calls them directly with scripted answers instead of
waiting for button taps. Consolidates what was previously "reseed, start server, tap
yes, tap a goal-time level, force a check-in, tap an answer, repeat N times, go check
list_agent_blocks.py each time" into one command.

Does NOT replace live testing — this still makes real Claude calls and real EventKit
writes against whatever TIMEBLOCK_RULES_PATH points at (sandbox or real — same safety
gate as run_day_start.py applies here too). What it removes is the manual Telegram
back-and-forth, not the need to actually look at the result.

Usage:
  .venv/bin/python scripts/run_automated_scenario.py [--reseed] [--goal-time none|light|balanced|heavy] \\
      [--checkin "<title substring>" --answer completed|running_behind|unexpected_plan [--repeat N]]

--reseed wipes and reseeds the sandbox first (equivalent to running
scripts/setup_test_sandbox.py), only ever safe against the sandbox — refuses if
TIMEBLOCK_RULES_PATH isn't set, same as setup_test_sandbox.py's own hardcoded-calendar
safety (it only ever touches "Test Bucket"/"Test Reminders"/"Task Blocks (Test)" by name,
but --reseed here still requires the sandbox env as a matter of not running this whole
script against real data by accident).

--checkin/--answer/--repeat re-find today's agent-authored block whose title CONTAINS the
given substring (case-insensitive) and calls run_checkin_answer() on it directly, N times
in a row (default 1) — printing the resulting blocks after each round, so a whole
cascade (e.g. Scenario 7's priority test) is one command instead of N manual Telegram
round-trips. Re-finds by title each round rather than reusing one identifier, since a
block's identifier can change across a replan.

Example (Scenario 7, in one command):
  .venv/bin/python scripts/run_automated_scenario.py --reseed --checkin "beta" --answer running_behind --repeat 3
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent import orchestrator
from timeblock_agent.config import load_rules
from timeblock_agent.day_layout import ProposedBlock
from timeblock_agent.diff_write import apply_layout
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH
from timeblock_agent.state import AgentState, save_state


def _confirm_real_data_run() -> bool:
    if os.environ.get("TIMEBLOCK_RULES_PATH"):
        return True
    print(
        "\n*** WARNING: no TIMEBLOCK_RULES_PATH set — this will run against your REAL "
        "rules.yaml, REAL calendars, and REAL reminders, and WILL WRITE real calendar "
        "events. ***\n"
        "If you meant to test in the sandbox, Ctrl+C now and run:\n"
        "  source scripts/sandbox_env.sh\n"
    )
    answer = input("Type 'yes' to proceed against REAL data, anything else to abort: ")
    return answer.strip().lower() == "yes"


def _place_beta_directly(bridge, rules, now: datetime) -> bool:
    """Compact mode's whole point is reaching the check-in cascade fast, not re-testing
    day-start's own model-driven placement (already covered by Scenario 2) — but that
    placement is genuinely non-deterministic, and caught live: with only one tiny
    reminder in a deliberately-small Test Work, the model sometimes leaves it entirely
    unscheduled rather than placing it, with nothing wrong to reject (a placement-quality
    choice, not a validation bug) — which then leaves nothing for --checkin to find.
    Sidesteps that variance entirely: places "Test task beta" directly, right after Test
    Meeting ends, with no model call at all. Returns False (and prints why) if the
    compact bucket shape isn't there to place it against.

    `now` is a required parameter (not `datetime.now()` internally) so a fake/synthetic
    clock (see fake_sandbox/) drives this the same way a real one does."""
    day_start = now.replace(hour=0, minute=0, second=0)
    day_end = now.replace(hour=23, minute=59, second=59)
    bucket_events = bridge.list_events(day_start, day_end, calendar_titles=["Test Bucket"])
    test_work = next((e for e in bucket_events if e.title == "Test Work"), None)
    test_meeting = next((e for e in bucket_events if e.title == "Test Meeting"), None)
    reminders = bridge.list_reminders(list_titles=["Test Reminders"])
    beta = next((r for r in reminders if r.title.startswith("Test task beta")), None)
    if test_work is None or test_meeting is None or beta is None:
        print("Compact placement: Test Work/Test Meeting/beta not found — did --reseed run with --compact?")
        return False

    start = test_meeting.end
    end = start + timedelta(minutes=20)
    if end > test_work.end:
        print(f"Compact placement: no room for beta ({start:%H:%M}-{end:%H:%M} exceeds Test Work's own end {test_work.end:%H:%M}).")
        return False

    block = ProposedBlock(
        bucket_event_id=test_work.identifier, title=beta.title,
        start=start, end=end, source="reminder", source_id=beta.identifier,
    )
    # Fetch what's actually there today (not `[]`) so re-running this without --reseed is
    # idempotent (update/unchanged) rather than creating a duplicate beta block alongside
    # whatever a prior invocation already placed.
    existing = bridge.list_events(day_start, day_end, calendar_titles=[rules.agent_calendar])
    apply_layout(bridge, [block], existing_agent_events=existing, agent_calendar=rules.agent_calendar, written_at=now)
    print(f"Placed 'Test task beta' directly: {start:%H:%M}-{end:%H:%M} (no model call)")
    return True


def _print_agent_blocks(bridge, rules, now: datetime):
    events = bridge.list_events(
        now.replace(hour=0, minute=0, second=0), now.replace(hour=23, minute=59, second=59),
        calendar_titles=[rules.agent_calendar],
    )
    for e in sorted(events, key=lambda e: e.start):
        print(f"  {e.start:%H:%M}-{e.end:%H:%M} {e.title!r}  [{e.identifier}]")


def run_rounds(
    bridge, rules, now: datetime, *,
    skip_day_start: bool = False,
    compact: bool = False,
    goal_time: str = "none",
    checkin: Optional[str] = None,
    answer: Optional[str] = None,
    repeat: int = 1,
    state_path=None,
) -> None:
    """The actual day-start/goal-time/compact-placement + --checkin/--answer/--repeat
    loop, extracted out of main() so a caller with its own bridge/clock (real or fake —
    see fake_sandbox/) can drive it directly in-process. `bridge`/`rules`/`now` are
    threaded through to every orchestrator call so a synthetic clock behaves exactly
    like a real one throughout — nothing in this function calls `datetime.now()` itself."""
    if skip_day_start:
        print("=== Skipping day-start/compact-placement — continuing from existing state ===")
    elif compact:
        # Skip day-start's model-driven placement entirely (non-deterministic for a
        # single tiny reminder in a deliberately-small bucket — see
        # _place_beta_directly) — compact mode's only job is reaching the check-in
        # cascade, not re-testing day-start placement quality (already covered
        # elsewhere). No goal-time prompt either — not relevant to this path.
        print("=== Compact mode: placing beta directly (skipping day-start's model call) ===")
        if not _place_beta_directly(bridge, rules, now):
            return
    else:
        print("=== Confirming day-start (yes) ===")
        trigger = orchestrator.handle_day_boundary(bridge, "day_start", "yes", state_path=state_path, now=now)
        print(f"-> next trigger: {trigger.reason} at {trigger.at}\n")

        print(f"=== Answering goal-time prompt ({goal_time}) ===")
        trigger = orchestrator.handle_goal_time_answer(bridge, goal_time, state_path=state_path, now=now)
        print(f"-> next trigger: {trigger.reason} at {trigger.at}\n")

    print("=== Resulting agent-authored blocks ===")
    _print_agent_blocks(bridge, rules, now)

    if checkin:
        for i in range(repeat):
            events = bridge.list_events(
                now.replace(hour=0, minute=0, second=0), now.replace(hour=23, minute=59, second=59),
                calendar_titles=[rules.agent_calendar],
            )
            match = next((e for e in events if checkin.lower() in e.title.lower()), None)
            if match is None:
                print(f"\nNo agent-authored block matching {checkin!r} found — stopping.")
                break
            print(f"\n=== Check-in {i + 1}/{repeat}: {answer!r} on {match.title!r} ({match.start:%H:%M}-{match.end:%H:%M}) ===")
            trigger = orchestrator.run_checkin_answer(bridge, match.identifier, answer, state_path=state_path, now=now)
            print(f"-> next trigger: {trigger.reason} at {trigger.at}")
            print("Resulting agent-authored blocks:")
            _print_agent_blocks(bridge, rules, now)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal-time", choices=["none", "light", "balanced", "heavy"], default="none")
    parser.add_argument("--reseed", action="store_true", help="Wipe and reseed the sandbox first")
    parser.add_argument(
        "--compact", action="store_true",
        help="Pass --compact through to setup_test_sandbox.py (scaled-down Work/Meeting/Hobby-only "
             "layout — auto-used anyway late in the day when the full 6h day wouldn't fit).",
    )
    parser.add_argument(
        "--skip-day-start", action="store_true",
        help="Skip day-start/compact-placement entirely and go straight to --checkin — "
             "for continuing more check-in rounds against whatever's already on the "
             "calendar from a prior invocation, without resetting it.",
    )
    parser.add_argument("--checkin", help="Title substring (case-insensitive) of the block to check in on")
    parser.add_argument("--answer", choices=["completed", "running_behind", "unexpected_plan"])
    parser.add_argument("--repeat", type=int, default=1, help="How many times to repeat the check-in answer")
    args = parser.parse_args()

    if args.checkin and not args.answer:
        print("--checkin requires --answer")
        return

    if not _confirm_real_data_run():
        print("Aborted.")
        return

    if args.reseed:
        print("=== Reseeding sandbox ===")
        seed_cmd = [sys.executable, str(Path(__file__).resolve().parent / "setup_test_sandbox.py")]
        if args.compact:
            seed_cmd.append("--compact")
        subprocess.run(seed_cmd, check=True)
        state_path = Path(os.environ.get("TIMEBLOCK_STATE_PATH", str(Path.home() / ".timeblock-agent" / "state.json")))
        save_state(AgentState())  # fresh state so day-start isn't already "confirmed" from an earlier run
        print(f"Reset state at {state_path}\n")

    rules = load_rules(RULES_PATH)
    bridge = EventKitBridge()
    cal_granted, rem_granted = bridge.request_access()
    if not (cal_granted and rem_granted):
        print("Calendar/Reminders access not granted.")
        return

    now = datetime.now()
    run_rounds(
        bridge, rules, now,
        skip_day_start=args.skip_day_start, compact=args.compact, goal_time=args.goal_time,
        checkin=args.checkin, answer=args.answer, repeat=args.repeat,
    )


if __name__ == "__main__":
    main()
