"""Zero-setup, fully offline CLI for exercising sandbox scenarios end to end against
`FakeEventKitBridge` instead of real Calendar.app/Reminders.app — no TCC access needed,
no `source scripts/sandbox_env.sh`, and no waiting for real wall-clock time to reach a
particular moment: `--now` jumps the clock to any instant instantly (including edge
cases like two minutes before `hard_cutoff`).

Mirrors `scripts/run_automated_scenario.py`'s flags exactly, reusing its (and
`scripts/setup_test_sandbox.py`'s) already-refactored `run_rounds()`/`run_seed()`
functions in-process rather than duplicating their logic — this file owns only the fake
bridge, the fake env-var wiring, and the `--now` clock, nothing else.

Deliberately does NOT fake the Claude API — classification/day-planning/cascade-replan
calls stay real, since observing the model's actual behavior is the whole point of this
testing effort; faking that away would defeat it. What this removes is Calendar/
Reminders hardware access and wall-clock waiting, nothing else. Model placement choices
still vary run to run through this path — expected, not a defect.

Usage:
  .venv/bin/python fake_sandbox/run_fake_scenario.py --now "2026-07-23 09:00" --reseed \\
      --compact --checkin "beta" --answer running_behind --repeat 3
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_SANDBOX_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_FAKE_SANDBOX_DIR))

_FAKE_HOME = Path.home() / ".timeblock-agent-fake"

# Every TIMEBLOCK_*_PATH constant is read ONCE at import time by the module that defines
# it, so this must run before any timeblock_agent import below — otherwise the fake tier
# would silently read/write real sandbox (or production) paths instead. setdefault (not
# a blind overwrite) so an explicit override already in the calling shell still wins.
os.environ.setdefault("TIMEBLOCK_RULES_PATH", str(_REPO_ROOT / "rules.test.yaml"))
os.environ.setdefault("TIMEBLOCK_BUCKETS_PATH", str(_REPO_ROOT / "buckets.test.md"))
os.environ.setdefault("TIMEBLOCK_STATE_PATH", str(_FAKE_HOME / "state.json"))
os.environ.setdefault("TIMEBLOCK_LOG_DIR", str(_FAKE_HOME / "logs"))
os.environ.setdefault("TIMEBLOCK_GOALS_DIR", str(_FAKE_HOME / "goals"))
os.environ.setdefault("TIMEBLOCK_TASK_PROGRESS_PATH", str(_FAKE_HOME / "task_progress.json"))
os.environ.setdefault("TIMEBLOCK_BLOCK_SNAPSHOTS_PATH", str(_FAKE_HOME / "block_snapshots.json"))
os.environ.setdefault("TIMEBLOCK_PREFERENCES_PATH", str(_FAKE_HOME / "preferences.md"))
os.environ.setdefault("TIMEBLOCK_FAKE_EVENTKIT_PATH", str(_FAKE_HOME / "fake_eventkit.json"))

import run_automated_scenario  # noqa: E402  (scripts/run_automated_scenario.py, flat import — scripts/ isn't a package)
import setup_test_sandbox  # noqa: E402      (scripts/setup_test_sandbox.py)
from fake_eventkit_bridge import FAKE_EVENTKIT_PATH, FakeEventKitBridge  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.state import AgentState, save_state  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--now", help='Synthetic clock, e.g. "2026-07-23 09:00" — defaults to real current time if omitted.'
    )
    parser.add_argument("--goal-time", choices=["none", "light", "balanced", "heavy"], default="none")
    parser.add_argument("--reseed", action="store_true", help="Wipe and reseed the fake sandbox first")
    parser.add_argument(
        "--compact", action="store_true",
        help="Pass --compact through to setup_test_sandbox.run_seed (scaled-down Work/Meeting/Hobby-only "
             "layout — auto-used anyway when the full 6h day wouldn't fit before the synthetic hard_cutoff).",
    )
    parser.add_argument(
        "--skip-day-start", action="store_true",
        help="Skip day-start/compact-placement entirely and go straight to --checkin — for continuing "
             "more check-in rounds against whatever's already in the persisted fake store.",
    )
    parser.add_argument("--checkin", help="Title substring (case-insensitive) of the block to check in on")
    parser.add_argument("--answer", choices=["completed", "running_behind", "unexpected_plan"])
    parser.add_argument("--repeat", type=int, default=1, help="How many times to repeat the check-in answer")
    args = parser.parse_args()

    if args.checkin and not args.answer:
        print("--checkin requires --answer")
        return

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") if args.now else datetime.now()

    bridge = FakeEventKitBridge()
    print(f"=== FAKE scenario (no real Calendar/Reminders touched) — store: {FAKE_EVENTKIT_PATH} ===")
    print(f"=== Synthetic clock: {now:%Y-%m-%d %H:%M} ===\n")

    rules = load_rules(RULES_PATH)

    if args.reseed:
        print("=== Reseeding fake sandbox ===")
        goals_dir = Path(os.environ["TIMEBLOCK_GOALS_DIR"])
        result = setup_test_sandbox.run_seed(bridge, rules, now, compact=args.compact, goals_dir=goals_dir)
        if result is None:
            return
        print(
            f"Reseed done — {'compact' if result.used_compact else 'full'} layout, "
            f"{result.available_minutes:.0f} synthetic minute(s) available before hard_cutoff.\n"
        )
        save_state(AgentState())  # fresh state so day-start isn't already "confirmed" from an earlier run

    run_automated_scenario.run_rounds(
        bridge, rules, now,
        skip_day_start=args.skip_day_start, compact=args.compact, goal_time=args.goal_time,
        checkin=args.checkin, answer=args.answer, repeat=args.repeat,
    )


if __name__ == "__main__":
    main()
