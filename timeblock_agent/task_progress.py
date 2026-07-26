"""Cross-day progress tracking for reminders too big to finish in one sitting (see
rules.md §2, "Multi-day tasks & due dates"). Deliberately its own file/lifecycle, not a
field on state.py's AgentState — task progress must survive across days on purpose,
unlike AgentState's fields, most of which reset on a fresh day_start_confirmed_date.

A task's `total_minutes` only exists once day_layout.plan_day() has actually estimated
it (an output, not an input) — so there's no entry here at all until the second day a
big task is seen. Day 1 always behaves like the existing best-effort single-day fit.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Optional

# TIMEBLOCK_TASK_PROGRESS_PATH lets a sandboxed test run keep its own progress file,
# separate from real tracked tasks — see TESTING.md.
DEFAULT_TASK_PROGRESS_PATH = Path(
    os.environ.get("TIMEBLOCK_TASK_PROGRESS_PATH", str(Path.home() / ".timeblock-agent" / "task_progress.json"))
)


@dataclass
class TaskProgress:
    reminder_id: str
    total_minutes: int
    completed_minutes: int
    due_date: Optional[date]
    first_seen: date

    def to_json(self) -> dict:
        d = asdict(self)
        d["due_date"] = self.due_date.isoformat() if self.due_date else None
        d["first_seen"] = self.first_seen.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "TaskProgress":
        return cls(
            reminder_id=d["reminder_id"],
            total_minutes=d["total_minutes"],
            completed_minutes=d["completed_minutes"],
            due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
            first_seen=date.fromisoformat(d["first_seen"]),
        )


def remaining_minutes(tp: TaskProgress) -> int:
    return max(tp.total_minutes - tp.completed_minutes, 0)


def is_complete(tp: TaskProgress) -> bool:
    return tp.completed_minutes >= tp.total_minutes


def is_overdue(tp: TaskProgress, today: date) -> bool:
    return tp.due_date is not None and tp.due_date < today


@dataclass
class PacingInfo:
    """Python-computed guidance for one due-dated reminder, fed into plan_day's prompt —
    the day-to-day pacing arithmetic is never left to the model (see
    orchestrator._compute_task_pacing)."""

    today_target_minutes: int
    remaining_minutes: int
    days_remaining: int
    at_risk: bool
    overdue: bool


def load_task_progress(path: Path = DEFAULT_TASK_PROGRESS_PATH) -> dict[str, TaskProgress]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {reminder_id: TaskProgress.from_json(d) for reminder_id, d in raw.items()}


def save_task_progress(progress: dict[str, TaskProgress], path: Path = DEFAULT_TASK_PROGRESS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({rid: tp.to_json() for rid, tp in progress.items()}, indent=2))
