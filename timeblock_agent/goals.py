"""Reads ~/goals/*.md (frontmatter + free-form notes) and picks a goal to fill spare time.

File shape (see templates/goal_template.md):

    ---
    title: ...
    priority: high | medium | low
    status: active | paused | done
    last_touched: YYYY-MM-DD
    next_action: ...
    ---
    ## Notes
    ...
    ## Log
    <!-- marker comment -->
    - past entries...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

from timeblock_agent.config import GoalFillWeights

logger = logging.getLogger("timeblock_agent.goals")

# TIMEBLOCK_GOALS_DIR lets a sandboxed test run use its own dummy goals, so exercising
# goal-fill during testing never bumps last_touched/appends log lines on your real goals
# — see TESTING.md.
DEFAULT_GOALS_DIR = Path(os.environ.get("TIMEBLOCK_GOALS_DIR", str(Path.home() / "goals")))
LOG_MARKER = "<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->"

_PRIORITY_SCORE = {"high": 1.0, "medium": 0.6, "low": 0.3}


class GoalError(RuntimeError):
    pass


@dataclass
class Goal:
    path: Path
    title: str
    priority: str
    status: str
    last_touched: date
    next_action: str


def _parse_frontmatter(text: str, path: Path) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise GoalError(f"{path}: missing frontmatter (must start with ---)")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise GoalError(f"{path}: malformed frontmatter (missing closing ---)")
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        raise GoalError(f"{path}: malformed frontmatter YAML: {e}") from e
    return fm, parts[2]


def _coerce_date(value, path: Path) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as e:
            raise GoalError(f"{path}: last_touched must be YYYY-MM-DD, got {value!r}") from e
    raise GoalError(f"{path}: last_touched must be a date, got {value!r}")


def _load_goal(path: Path) -> Goal:
    fm, _ = _parse_frontmatter(path.read_text(), path)
    for field in ("title", "priority", "status", "last_touched", "next_action"):
        if field not in fm:
            raise GoalError(f"{path}: missing required frontmatter field: {field}")
    if fm["priority"] not in _PRIORITY_SCORE:
        raise GoalError(f"{path}: priority must be one of {list(_PRIORITY_SCORE)}, got {fm['priority']!r}")
    return Goal(
        path=path,
        title=fm["title"],
        priority=fm["priority"],
        status=fm["status"],
        last_touched=_coerce_date(fm["last_touched"], path),
        next_action=fm["next_action"],
    )


def list_goals(goals_dir: Path = DEFAULT_GOALS_DIR) -> list[Goal]:
    """Skips (with a logged warning) any .md file that isn't a valid goal — e.g. a
    stray copy of an unrelated document dropped into the folder — rather than letting
    one bad file take down every other real goal."""
    if not goals_dir.exists():
        raise GoalError(f"Goals directory not found: {goals_dir}")

    goals = []
    for path in sorted(goals_dir.glob("*.md")):
        try:
            goals.append(_load_goal(path))
        except GoalError as e:
            logger.warning("Skipping %s: %s", path, e)
    return goals


def pick_goal(
    goals: list[Goal],
    weights: GoalFillWeights,
    today: Optional[date] = None,
    exclude: Optional[set[Path]] = None,
) -> Optional[Goal]:
    """Weighted pick among active goals, favoring stale + high-priority. None if no
    active goals remain once `exclude` (goal `path`s already fully allocated elsewhere
    this cycle — see allocate_goal_sessions) is applied."""
    today = today or date.today()
    exclude = exclude or set()
    active = [g for g in goals if g.status == "active" and g.path not in exclude]
    if not active:
        return None

    max_staleness_days = max((today - g.last_touched).days for g in active) or 1

    def score(g: Goal) -> float:
        staleness_norm = (today - g.last_touched).days / max_staleness_days
        priority_norm = _PRIORITY_SCORE[g.priority]
        return weights.staleness * staleness_norm + weights.priority * priority_norm

    return max(active, key=score)


@dataclass
class GoalSession:
    goal: Goal
    minutes: int


def allocate_goal_sessions(
    goals: list[Goal], weights: GoalFillWeights, budget_minutes: int, max_minutes_per_goal: int
) -> list[GoalSession]:
    """Splits a day's goal-time budget across one or more goals, deterministically —
    never asks a model to manage this. Repeatedly picks the highest-scoring active goal
    not yet fully allocated this call, credits it up to `max_minutes_per_goal`, and moves
    on to the next-best goal for whatever's left, until the budget is spent or there are
    no more active goals to pick from (see rules.md §2, "More than one goal in a day")."""
    if budget_minutes <= 0:
        return []

    sessions: list[GoalSession] = []
    exclude: set[Path] = set()
    remaining = budget_minutes

    while remaining > 0:
        candidate = pick_goal(goals, weights, exclude=exclude)
        if candidate is None:
            break
        minutes = min(remaining, max_minutes_per_goal)
        sessions.append(GoalSession(goal=candidate, minutes=minutes))
        remaining -= minutes
        exclude.add(candidate.path)

    return sessions


def record_completion(goal: Goal, entry: str, when: Optional[datetime] = None) -> None:
    """Appends a log line under the `## Log` marker and bumps last_touched to today."""
    when = when or datetime.now()
    text = goal.path.read_text()
    fm, body = _parse_frontmatter(text, goal.path)

    fm["last_touched"] = when.date().strftime("%Y-%m-%d")
    log_line = f"- {when:%Y-%m-%d %H:%M}: {entry}"

    if LOG_MARKER in body:
        body = body.replace(LOG_MARKER, f"{LOG_MARKER}\n{log_line}", 1)
    else:
        body = body.rstrip("\n") + f"\n\n## Log\n{LOG_MARKER}\n{log_line}\n"

    new_frontmatter = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    goal.path.write_text(f"---\n{new_frontmatter}\n---{body}")
