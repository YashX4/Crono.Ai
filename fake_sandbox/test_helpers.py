"""Shared setup for fake-sandbox automated tests.

`setup_fake_env(tmp_dir)` MUST be called BEFORE any `timeblock_agent` import in the
calling file — every TIMEBLOCK_*_PATH constant those modules read (and
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, real values in this repo's .env) is bound ONCE at
first import (see fake_eventkit_bridge.py / run_fake_scenario.py for the same pattern).

Files that never import `timeblock_agent.orchestrator` (pure day_layout.py/goals.py/
incremental_replan.py callers — see EDGE_CASES.md's tier legend) don't need this at all.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "scripts"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def setup_fake_env(tmp_dir: Path) -> None:
    """Hard-overrides (not setdefault) every path-bearing env var into `tmp_dir`, plus
    Telegram credentials to blank. Hard override (unlike run_fake_scenario.py's
    setdefault, which deliberately lets an interactive shell's override win) since this
    is meant to be hermetic every run regardless of what's left over in the environment
    — an automated test must never depend on, or leak into, whatever's already set."""
    tmp_dir = Path(tmp_dir)
    os.environ["TIMEBLOCK_RULES_PATH"] = str(_REPO_ROOT / "rules.test.yaml")
    os.environ["TIMEBLOCK_BUCKETS_PATH"] = str(_REPO_ROOT / "buckets.test.md")
    os.environ["TIMEBLOCK_STATE_PATH"] = str(tmp_dir / "state.json")
    os.environ["TIMEBLOCK_LOG_DIR"] = str(tmp_dir / "logs")
    os.environ["TIMEBLOCK_GOALS_DIR"] = str(tmp_dir / "goals")
    os.environ["TIMEBLOCK_TASK_PROGRESS_PATH"] = str(tmp_dir / "task_progress.json")
    os.environ["TIMEBLOCK_BLOCK_SNAPSHOTS_PATH"] = str(tmp_dir / "block_snapshots.json")
    os.environ["TIMEBLOCK_PREFERENCES_PATH"] = str(tmp_dir / "preferences.md")
    os.environ["TIMEBLOCK_FAKE_EVENTKIT_PATH"] = str(tmp_dir / "fake_eventkit.json")
    (tmp_dir / "goals").mkdir(parents=True, exist_ok=True)  # list_goals() raises if absent
    # Real, live values are configured in this repo's .env for manual live testing — must
    # never reach a real phone from an automated run. Must win over telegram_notifier.py's
    # own load_dotenv(), which only runs the moment orchestrator.py is first imported
    # after this — so this MUST run before that import, every time, no exceptions.
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TELEGRAM_CHAT_ID"] = ""


class TraceLogger:
    """Every check goes through here instead of a bare `assert` — logs [PASS]/[FAIL]
    with both values regardless of outcome, flushes after every check (not just at exit,
    so a crash mid-test doesn't lose the trail), and raises AssertionError pointing at
    the log file on failure. `step`/`call` record setup actions and function calls with
    their key inputs, so the full narrative survives on disk after every run."""

    def __init__(self, test_name: str, log_dir: Optional[Path] = None):
        log_dir = Path(log_dir) if log_dir else (Path(__file__).resolve().parent / "test_logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{test_name}.log"
        self._lines: list[str] = [f"=== {test_name} — {datetime.now().isoformat()} ==="]
        self._failures = 0
        self.flush()

    def step(self, msg: str) -> None:
        self._lines.append(f"[STEP] {msg}")
        self.flush()

    def call(self, fn_name: str, **kwargs) -> None:
        rendered = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        self._lines.append(f"[CALL] {fn_name}({rendered})")
        self.flush()

    def note(self, msg: str) -> None:
        self._lines.append(f"[NOTE] {msg}")
        self.flush()

    def check(self, description: str, expected, actual) -> bool:
        ok = expected == actual
        self._lines.append(f"[{'PASS' if ok else 'FAIL'}] {description}: expected={expected!r} actual={actual!r}")
        self.flush()
        if not ok:
            self._failures += 1
            raise AssertionError(f"{description}: expected {expected!r}, got {actual!r} (see {self.path})")
        return ok

    def check_true(self, description: str, condition: bool, detail: str = "") -> bool:
        return self.check(description + (f" ({detail})" if detail else ""), True, bool(condition))

    def flush(self) -> None:
        self.path.write_text("\n".join(self._lines) + "\n")

    def finish(self, ok: bool = True) -> None:
        self._lines.append(f"=== {'ALL CASES PASSED' if ok else 'FAILED'} ===")
        self.flush()


# ---- Safe from ANY tier — no orchestrator import, no env binding of consequence ----

from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.bucket_context import load_bucket_context  # noqa: E402
from timeblock_agent.eventkit_bridge import CalendarEvent, Reminder  # noqa: E402


def load_test_rules():
    return load_rules(_REPO_ROOT / "rules.test.yaml")


def load_test_bucket_context() -> str:
    return load_bucket_context(_REPO_ROOT / "buckets.test.md")


def make_event(
    identifier: str,
    title: str,
    start: datetime,
    end: datetime,
    calendar_title: str = "Test Bucket",
    is_all_day: bool = False,
    notes: Optional[str] = None,
    has_attendees: bool = False,
) -> CalendarEvent:
    return CalendarEvent(
        identifier=identifier, title=title, start=start, end=end, is_all_day=is_all_day,
        calendar_identifier="cal1", calendar_title=calendar_title, location=None, notes=notes,
        url=None, has_attendees=has_attendees,
    )


def make_reminder(
    identifier: str,
    title: str,
    due_date: Optional[datetime] = None,
    priority: int = 5,
    list_title: str = "Test Reminders",
) -> Reminder:
    return Reminder(
        identifier=identifier, title=title, notes=None, due_date=due_date, priority=priority,
        completed=False, list_identifier="rlist1", list_title=list_title,
    )


def make_capturing_wrapper(real_fn):
    """Returns (wrapper, calls) — wrapper calls `real_fn` for real and appends
    {"args", "kwargs", "result"} to `calls` on every invocation. Use with
    `mock.patch(target, side_effect=wrapper)` to spy on a function's real calls/return
    values without relying on any non-standard Mock attribute (plain `wraps=` alone
    doesn't expose the wrapped call's return value conveniently to the caller)."""
    calls: list[dict] = []

    def wrapper(*args, **kwargs):
        result = real_fn(*args, **kwargs)
        calls.append({"args": args, "kwargs": kwargs, "result": result})
        return result

    return wrapper, calls


def retry(attempts: int, fn, trace: TraceLogger, label: str):
    """Bounded-retry wrapper for real-model-driven cases — real Claude Haiku calls are
    known non-determinstic (see TESTING_LOG.md's Scenario 7/8 manual runs, which needed
    1-3 attempts to land in a specific condition). Re-raises the LAST attempt's
    AssertionError (with the earlier attempts noted in the trace) if every attempt fails."""
    last_error: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            result = fn()
            trace.note(f"{label}: PASS on attempt {i}/{attempts}")
            return result
        except AssertionError as e:
            last_error = e
            trace.note(f"{label}: attempt {i}/{attempts} failed — {e}")
    raise last_error
