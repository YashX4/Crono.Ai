"""Resets and reseeds the test sandbox (see TESTING.md) so a full scenario checklist can
be run repeatably in one sitting without touching real Calendar/Reminders data.

Wipes today's events in "Test Bucket" and "Task Blocks (Test)", and all reminders in
"Test Reminders", then recreates a fresh, deliberately-shaped test day:

  - Test Work      09:00-11:00  (FLEXIBLE_BUCKET, meant to read as higher priority,
                                 goal-time-eligible)
  - Test Meeting   10:30-10:45  (FIXED, sits INSIDE Test Work's window on purpose —
                                 verifies FIXED overlap protection survives a cascade)
  - Test Hobby     11:00-14:00  (FLEXIBLE_BUCKET, meant to read as lower priority,
                                 directly adjacent to Test Work so eat-the-next-bucket
                                 cascading is observable, NOT goal-time-eligible; 3h long
                                 so it can genuinely shrink-supply a full "Balanced"
                                 150-min goal-time budget on its own, not just a partial
                                 amount capped by its own small size)
  - Test Gym       14:00-14:30  (FLEXIBLE_SPECIFIC)
  - Test Buffer    14:30-15:00  (protected buffer, goal-fill eligible)

Plus 5 open reminders in "Test Reminders" — the first three (~70 min combined) leave
Test Work with real natural slack for the default goal-time-sourcing scenario; the
fourth ("Test task delta", ~90 min) is sized to deliberately overflow whatever room is
left, to exercise the bucket-extension-for-real-overflow path (see TESTING.md); the fifth
("Test task epsilon", ~6h, real `due_date` 3 days out) exercises multi-day
pacing/carryover — deliberately too big to matter how much room Test Work has. Comment
any of these out (or just answer with fewer reminders in mind) if you want a cleaner run
of one specific scenario without the others also triggering.

Plus TWO dummy goals (so the multi-goal-session-split scenario is exercisable — see
TESTING.md; rules.test.yaml's max_minutes_per_goal is set low specifically so a
"Balanced" ~150min ask exceeds one goal's cap and must split across both).

All test times are set relative to whenever this script is run, so it's safe to run it
fresh at the start of each testing session. Must run from Terminal.app (EventKit access).

The full-size day above needs ~6 real hours of room before today's hard cutoff
(`evening_checkin_floor`, always combined with TODAY's date — hard_cutoff can never
reach into tomorrow) — running this late in the evening leaves nowhere near enough real
time for the whole thing to fit, and anything past hard_cutoff is structurally
unreachable by any cascade regardless of bucket sizing (caught live: seeded at 22:20,
Test Work/Meeting/Hobby/Gym/Buffer spanned past midnight, and every "still going" attempt
hit the 23:59 wall long before ever reaching Test Work's own boundary). `--compact` (or
automatic, if there genuinely isn't room for the full day) scales down to whatever real
time is actually left: Test Meeting/Work/Hobby + a single reminder (beta) only
(Gym/Buffer/alpha/gamma/delta/epsilon dropped — not needed to exercise the cascade),
sized to fit with a safety margin, so bucket-boundary/priority-cascade testing isn't
limited to daytime hours.

Usage: .venv/bin/python scripts/setup_test_sandbox.py [--compact]
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.config import Rules, load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.orchestrator import RULES_PATH

TEST_BUCKET_CAL = "Test Bucket"
TEST_AGENT_CAL = "Task Blocks (Test)"
TEST_REMINDER_LIST = "Test Reminders"
TEST_GOALS_DIR = Path.home() / "timeblock-test-goals"

# Full-size layout needs Meeting+Work+Hobby+Gym+Buffer = 6h; pad for buffers/rounding.
_FULL_DAY_MINUTES = 6 * 60 + 30
_SAFETY_MARGIN_MINUTES = 10  # never schedule right up against hard_cutoff itself
_COMPACT_MEETING_MINUTES = 3
_COMPACT_BETA_MINUTES = 20
_COMPACT_WORK_FLOOR_MINUTES = _COMPACT_MEETING_MINUTES + _COMPACT_BETA_MINUTES + 7  # + a little real slack
_COMPACT_HOBBY_FLOOR_MINUTES = 10  # enough to make an eat-into/push-later decision observable at all
# Below this, even compact mode can't fit its own minimum floors (Work + Hobby + margin).
_MIN_VIABLE_MINUTES = _COMPACT_WORK_FLOOR_MINUTES + _COMPACT_HOBBY_FLOOR_MINUTES + _SAFETY_MARGIN_MINUTES

TEST_GOAL_CONTENT = """\
---
title: Test goal A — safe to fill Test Buffer / Goal Time with
priority: medium
status: active
last_touched: 2020-01-01
next_action: Do test goal A's next action
---

## Notes

A dummy goal for sandbox testing — see TESTING.md. last_touched is set far in the past
so it wins the staleness-weighted pick first, ahead of Test goal B.

## Log

<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->
"""

TEST_GOAL_CONTENT_2 = """\
---
title: Test goal B — the second pick once goal A is capped
priority: low
status: active
last_touched: 2020-06-01
next_action: Do test goal B's next action
---

## Notes

A second dummy goal, less stale than Test goal A, so it only gets picked once A hits
`max_minutes_per_goal` and gets excluded — exercises the multi-goal-session split.

## Log

<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->
"""


def _reset_test_goals_dir(goals_dir: Path = TEST_GOALS_DIR) -> None:
    # Parameterized (not just TEST_GOALS_DIR unconditionally) so a caller pointing
    # TIMEBLOCK_GOALS_DIR somewhere else (e.g. fake_sandbox/'s dedicated fake directory)
    # doesn't silently write into the real sandbox's ~/timeblock-test-goals regardless.
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / "test-goal-a.md").write_text(TEST_GOAL_CONTENT)
    (goals_dir / "test-goal-b.md").write_text(TEST_GOAL_CONTENT_2)
    print(f"Reset 2 test goals in {goals_dir}")


def _wipe_calendar(bridge: EventKitBridge, calendar_title: str, day_start: datetime, day_end: datetime) -> int:
    existing = bridge.list_events(day_start, day_end, calendar_titles=[calendar_title])
    for ev in existing:
        bridge.delete_event(ev.identifier)
    return len(existing)


def _wipe_reminders(bridge: EventKitBridge, list_title: str) -> int:
    existing = bridge.list_reminders(list_titles=[list_title], include_completed=True)
    for r in existing:
        bridge.delete_reminder(r.identifier)
    return len(existing)


def _full_layout(base: datetime, now: datetime):
    events = [
        ("Test Work", base, base + timedelta(hours=2)),
        # Sits INSIDE Test Work's window on purpose — verifies FIXED overlap protection
        # survives a cascade. 30 min before Test Work's own end: far enough in that a
        # single "still going" extension chain reaching it doesn't ALSO immediately need
        # a bucket_adjustments extension in the same step (keeps that path distinct from
        # the bucket-boundary/priority-cascade one, which needs the full day's real time —
        # see `_compact_layout` for the version that skips this tension entirely by
        # dropping the meeting and gamma/delta/epsilon).
        ("Test Meeting", base + timedelta(hours=1, minutes=30), base + timedelta(hours=1, minutes=45)),
        # 3h, not 1h — big enough to genuinely shrink-supply a full "Balanced" (150 min)
        # goal-time budget on its own (retaining a real 30+ min of its own time), rather
        # than being smaller than the request regardless of code correctness (caught
        # live: with only 1h here, the shrink path could only ever source ~50-80 min no
        # matter what, silently under-delivering the requested 150 with no way to fully
        # verify the multi-goal-split path through this fallback route).
        ("Test Hobby", base + timedelta(hours=2), base + timedelta(hours=5)),
        ("Test Gym", base + timedelta(hours=5), base + timedelta(hours=5, minutes=30)),
        ("Test Buffer", base + timedelta(hours=5, minutes=30), base + timedelta(hours=6)),
    ]
    # Notes are sent to the model as real input (same as any real reminder's notes would
    # be) — deliberately plain, natural-sounding task descriptions with NO mention of
    # testing/scenarios/scripts. Meta-commentary here (what this reminder is "for") would
    # leak our own test intent into the model's actual reasoning, which a real reminder
    # never would, and can visibly skew results (confirmed live: an earlier version of
    # this file's notes correlated with the model treating a task as flexibly-sized
    # rather than realistically-sized). What each one is *for* is explained here in code
    # comments and in TESTING.md instead, never in the `notes` text itself.
    reminders = [
        # ~20 min, quick/low-effort — leaves Test Work with real natural slack for the
        # default goal-time-sourcing scenario.
        ("Test task alpha (~20 min)", "Quick email to send about the internship follow-up.", None),
        # ~20 min, quick/low-effort.
        ("Test task beta (~20 min)", "Update the resume with the latest project.", None),
        # ~30 min, a bit more involved.
        ("Test task gamma (~30 min)", "Review and reply to a few pending messages.", None),
        # ~90 min — deliberately sized (via the title's own duration hint) to overflow
        # whatever room is left in Test Work after alpha/beta/gamma — exercises the
        # bucket-extension-for-real-overflow path (see TESTING.md).
        ("Test task delta (~90 min)", "Write the first draft of the project proposal document.", None),
        # ~6 hours, due in 3 days — deliberately too big for one day, exercises
        # multi-day pacing/carryover (see TESTING.md).
        (
            "Test task epsilon (~6 hours)",
            "Finish the full project report before the deadline.",
            now + timedelta(days=3),
        ),
    ]
    return events, reminders


def _compact_layout(base: datetime, available_minutes: float):
    """Scaled-down layout for exercising the bucket-boundary/priority-cascade path
    (Scenario 7) regardless of time of day — Gym/Buffer/alpha/gamma/delta/epsilon dropped
    entirely (not needed for the cascade itself — that sibling-cascade/multi-reminder
    territory is already covered live and in the mocked regression suite — and every real
    minute is precious when there isn't much room left before hard_cutoff; keeping just
    one reminder, beta, keeps Test Work's own minimum footprint as small as possible).
    Test Meeting sits at the very front of Test Work (not 30 min before its end, unlike
    the full layout) — a single contiguous task block can never validly span across a
    FIXED event sitting in its path (confirmed live), so with the meeting anywhere but
    the front, a "still going" chain permanently caps out there and can never reach Test
    Work's own boundary at all.

    Test Work gets just enough for the meeting + beta + a little slack (a fixed floor,
    not scaled down further even when room is tight — shrinking beta's own container
    below what it needs would just trade one blocker for another); whatever's left after
    that (still respecting Hobby's own floor) goes to Test Hobby, so the adjacent-bucket
    priority decision is still genuinely observable rather than a sliver. Assumes the
    caller already checked `available_minutes >= _MIN_VIABLE_MINUTES`."""
    usable = available_minutes - _SAFETY_MARGIN_MINUTES
    # Give Work extra room when there's plenty to spare (a more realistic-looking bucket
    # for a normal-daytime compact run), but never let it eat into Hobby's own floor.
    work_minutes = max(_COMPACT_WORK_FLOOR_MINUTES, min(usable * 0.5, 90))
    work_minutes = min(work_minutes, usable - _COMPACT_HOBBY_FLOOR_MINUTES)
    hobby_minutes = usable - work_minutes
    work_start = base
    meeting_end = work_start + timedelta(minutes=_COMPACT_MEETING_MINUTES)
    work_end = work_start + timedelta(minutes=work_minutes)
    hobby_end = work_end + timedelta(minutes=hobby_minutes)
    events = [
        ("Test Work", work_start, work_end),
        ("Test Meeting", work_start, meeting_end),
        ("Test Hobby", work_end, hobby_end),
    ]
    reminders = [
        ("Test task beta (~20 min)", "Update the resume with the latest project.", None),
    ]
    return events, reminders


@dataclass
class SeedResult:
    used_compact: bool
    available_minutes: float


def run_seed(
    bridge: EventKitBridge, rules: Rules, now: datetime,
    compact: bool = False, goals_dir: Path = TEST_GOALS_DIR,
) -> Optional[SeedResult]:
    """The actual reseed logic, extracted out of main() so a caller with its own
    bridge/clock (real or fake — see fake_sandbox/) can drive it directly in-process,
    without needing to also own a CLI/argparse or a real EventKitBridge/datetime.now().
    Returns None (and prints why) if wiping failed or there wasn't enough real room —
    the caller should treat that the same as main() returning early today."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    for cal in (TEST_BUCKET_CAL, TEST_AGENT_CAL):
        try:
            n = _wipe_calendar(bridge, cal, day_start, day_end)
            print(f"Wiped {n} existing event(s) from {cal!r}")
        except Exception as e:
            print(f"Could not wipe {cal!r} — does the calendar exist yet? ({e})")
            print(f"Create it once in Calendar.app (a plain local calendar, not iCloud-shared) and re-run.")
            return None

    try:
        n = _wipe_reminders(bridge, TEST_REMINDER_LIST)
        print(f"Wiped {n} existing reminder(s) from {TEST_REMINDER_LIST!r}")
    except Exception as e:
        print(f"Could not wipe {TEST_REMINDER_LIST!r} — does the list exist yet? ({e})")
        print("Create it once in Reminders.app and re-run.")
        return None

    # Round up to the next full hour for clean block boundaries — unless that would push
    # the whole synthetic day onto tomorrow's date (running this late at night), in which
    # case anchor to a couple minutes from now instead. The day-start pass only ever
    # queries through the rest of TODAY, so a day that starts tomorrow would be invisible
    # to it entirely; a day that merely *ends* after midnight is still fine (EventKit's
    # query matches on overlap, not full containment).
    base = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if base.date() != now.date():
        base = now + timedelta(minutes=2)

    # hard_cutoff (evening_checkin_floor) is always combined with TODAY's date — it can
    # never reach into tomorrow, so anything past it is structurally unreachable by any
    # cascade no matter how buckets are sized. Decide full-vs-compact from how much real
    # room is actually left before it, rather than assuming there's always a full evening
    # available (caught live: seeded at 22:20, the full-size day spanned past midnight
    # and every "still going" attempt hit the wall long before reaching Test Work's own
    # boundary).
    hard_cutoff = datetime.combine(base.date(), rules.evening_checkin_floor)
    available_minutes = (hard_cutoff - base).total_seconds() / 60

    if available_minutes < _MIN_VIABLE_MINUTES:
        print(
            f"\nOnly {available_minutes:.0f} real minute(s) left before today's hard_cutoff "
            f"({rules.evening_checkin_floor}) — not enough room for even a compact layout. "
            "Try again tomorrow, or once past a real day boundary."
        )
        return None

    use_compact = compact or available_minutes < _FULL_DAY_MINUTES
    if use_compact:
        events, reminders = _compact_layout(base, available_minutes)
        print(
            f"\n=== COMPACT layout ({available_minutes:.0f} real min available before "
            f"hard_cutoff {rules.evening_checkin_floor}) — Work/Meeting/Hobby + beta only, "
            "alpha/gamma/delta/epsilon/Gym/Buffer skipped ===\n"
        )
    else:
        events, reminders = _full_layout(base, now)

    for title, start, end in events:
        bridge.create_event(title, start, end, calendar_title=TEST_BUCKET_CAL)
        print(f"Created {title!r} {start:%H:%M}-{end:%H:%M}")

    for title, notes, due_date in reminders:
        bridge.create_reminder(title, list_title=TEST_REMINDER_LIST, notes=notes, due_date=due_date)
        print(f"Created reminder {title!r}" + (f" (due {due_date:%Y-%m-%d})" if due_date else ""))

    _reset_test_goals_dir(goals_dir)

    return SeedResult(used_compact=use_compact, available_minutes=available_minutes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compact", action="store_true",
        help="Force the scaled-down Work/Meeting/Hobby-only layout (auto-used anyway if "
             "the full 6h day wouldn't fit before today's hard_cutoff).",
    )
    args = parser.parse_args()

    bridge = EventKitBridge()
    cal_granted, rem_granted = bridge.request_access()
    if not (cal_granted and rem_granted):
        print("Calendar/Reminders access not granted.")
        return

    now = datetime.now()
    rules = load_rules(RULES_PATH)
    result = run_seed(bridge, rules, now, compact=args.compact)
    if result is None:
        return

    print("\nSandbox reset. Run the server with:")
    print(
        "  TIMEBLOCK_RULES_PATH=rules.test.yaml \\\n"
        "  TIMEBLOCK_STATE_PATH=$HOME/.timeblock-agent/state.test.json \\\n"
        "  TIMEBLOCK_LOG_DIR=$HOME/timeblock-test-logs \\\n"
        "  TIMEBLOCK_GOALS_DIR=$HOME/timeblock-test-goals \\\n"
        "  TIMEBLOCK_BUCKETS_PATH=buckets.test.md \\\n"
        "  TIMEBLOCK_TASK_PROGRESS_PATH=$HOME/.timeblock-agent/task_progress.test.json \\\n"
        "  TIMEBLOCK_BLOCK_SNAPSHOTS_PATH=$HOME/.timeblock-agent/block_snapshots.test.json \\\n"
        "  TIMEBLOCK_PREFERENCES_PATH=$HOME/timeblock-test-preferences.md \\\n"
        "    .venv/bin/python -m uvicorn timeblock_agent.server:app --host 0.0.0.0 --port 8787"
    )


if __name__ == "__main__":
    main()
