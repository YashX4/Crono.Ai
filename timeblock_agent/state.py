"""Persisted scheduler state + next-trigger computation (see spec: "Sleep/wake handling" /
"Dynamic scheduler: computes next trigger as min(next block end, pending follow-up,
hourly fallback, evening floor)").

This is the one thing that has to survive between runs of the (not-yet-built) persistent
process — everything else (what's on the calendar, what's in Reminders) is re-read fresh
each time rather than cached, per the spec's "read only what changed" incremental model.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from timeblock_agent.config import Rules

# TIMEBLOCK_STATE_PATH lets a sandboxed test run keep its own day-start/day-end
# confirmation + dedup state separate from real production state — see TESTING.md.
DEFAULT_STATE_PATH = Path(
    os.environ.get("TIMEBLOCK_STATE_PATH", str(Path.home() / ".timeblock-agent" / "state.json"))
)

FOLLOWUP_REASONS = ("unexpected", "continuing")
TRIGGER_REASONS = (
    "block_end", "block_start", "followup", "morning_floor", "morning_floor_retry",
    "evening_floor", "evening_floor_retry", "weekly_review", "weekly_review_retry",
    "safety_backstop",
)


@dataclass
class PendingReminderIntake:
    """An open conversational-reminder-intake thread (see rules.md §10) — the running
    transcript so far. `messages` holds plain {"role": "user"|"assistant", "content": str}
    text turns (never tool_use/tool_result blocks), replayed straight into the next
    reminder_intake.resolve() call. `last_activity_at` (not `started_at`) is the timeout
    anchor, since the design is "expires if you never reply," not "expires N minutes
    after opening" — a reply keeps the thread alive indefinitely.

    Stays open even after a "ready" draft is confirmed-shown (not just while still
    clarifying) — a reply at that point is treated as feedback to revise the draft, not a
    new unrelated request, so the conversation only actually ends on an explicit
    Yes/Cancel tap or a plain-language cancel. `active_confirm_token` tracks the most
    recently shown confirmation's callback token so a revision can invalidate the now-stale
    one before showing a new one — otherwise a later tap on the old buttons could still
    create the reminder with outdated values."""

    messages: list[dict]
    started_at: datetime
    last_activity_at: datetime
    active_confirm_token: Optional[str] = None


@dataclass
class AgentState:
    last_run_at: Optional[datetime] = None
    pending_followup_at: Optional[datetime] = None
    pending_followup_reason: Optional[str] = None  # "unexpected" | "continuing"
    day_start_confirmed_date: Optional[date] = None
    day_end_confirmed_date: Optional[date] = None
    # True between "yes, I'm up" and the goal-time budget answer that follows it — day
    # start isn't actually confirmed (day_start_confirmed_date isn't set) until that
    # second answer lands, but we don't want to re-ask "are you up?" in the meantime.
    day_start_awaiting_goal_time: bool = False
    # Agent-authored block identifiers answered "completed" today. Their calendar time
    # never changes (nothing to reschedule once done), so without this, the same block
    # would keep looking "overdue" and re-trigger a check-in notification on every tick
    # forever. Cleared on a fresh day_start_confirmed_date.
    resolved_block_ids: set[str] = field(default_factory=set)
    # Block identifiers whose "starting now" FYI has already been sent today — without
    # this, a long block would get re-announced as "starting" on every tick that finds it
    # still active. Cleared on a fresh day_start_confirmed_date, same as resolved_block_ids.
    started_block_ids: set[str] = field(default_factory=set)
    # Seeded to "now" the first time a day-start ever confirms (not left None forever) —
    # so the very first weekly review is due a week after real usage starts, not
    # immediately on a fresh install with zero data to say anything about.
    last_weekly_review_at: Optional[datetime] = None
    # Set via POST /resume, cleared via POST /pause (see menu_bar.py's "Agent Active"
    # toggle) — while True, run_scheduled_tick's own caller (server.py) never schedules
    # another tick at all: no check-ins, no day-start, nothing, until resumed.
    agent_paused: bool = False
    # An open conversational-reminder-intake thread awaiting the user's reply, or None if
    # no clarification is in progress. See PendingReminderIntake above.
    pending_reminder_intake: Optional[PendingReminderIntake] = None

    def to_json(self) -> dict:
        d = asdict(self)
        for key in ("last_run_at", "pending_followup_at", "last_weekly_review_at"):
            if d[key] is not None:
                d[key] = d[key].isoformat()
        for key in ("day_start_confirmed_date", "day_end_confirmed_date"):
            if d[key] is not None:
                d[key] = d[key].isoformat()
        d["resolved_block_ids"] = sorted(self.resolved_block_ids)
        d["started_block_ids"] = sorted(self.started_block_ids)
        if self.pending_reminder_intake is not None:
            d["pending_reminder_intake"] = {
                "messages": self.pending_reminder_intake.messages,
                "started_at": self.pending_reminder_intake.started_at.isoformat(),
                "last_activity_at": self.pending_reminder_intake.last_activity_at.isoformat(),
                "active_confirm_token": self.pending_reminder_intake.active_confirm_token,
            }
        else:
            d["pending_reminder_intake"] = None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "AgentState":
        pri = d.get("pending_reminder_intake")
        return cls(
            last_run_at=datetime.fromisoformat(d["last_run_at"]) if d.get("last_run_at") else None,
            pending_followup_at=(
                datetime.fromisoformat(d["pending_followup_at"]) if d.get("pending_followup_at") else None
            ),
            pending_followup_reason=d.get("pending_followup_reason"),
            resolved_block_ids=set(d.get("resolved_block_ids", [])),
            started_block_ids=set(d.get("started_block_ids", [])),
            day_start_confirmed_date=(
                date.fromisoformat(d["day_start_confirmed_date"]) if d.get("day_start_confirmed_date") else None
            ),
            day_end_confirmed_date=(
                date.fromisoformat(d["day_end_confirmed_date"]) if d.get("day_end_confirmed_date") else None
            ),
            day_start_awaiting_goal_time=d.get("day_start_awaiting_goal_time", False),
            last_weekly_review_at=(
                datetime.fromisoformat(d["last_weekly_review_at"]) if d.get("last_weekly_review_at") else None
            ),
            agent_paused=d.get("agent_paused", False),
            pending_reminder_intake=(
                PendingReminderIntake(
                    messages=pri["messages"],
                    started_at=datetime.fromisoformat(pri["started_at"]),
                    last_activity_at=datetime.fromisoformat(pri["last_activity_at"]),
                    active_confirm_token=pri.get("active_confirm_token"),
                )
                if pri
                else None
            ),
        )


def load_state(path: Path = DEFAULT_STATE_PATH) -> AgentState:
    if not path.exists():
        return AgentState()
    return AgentState.from_json(json.loads(path.read_text()))


def save_state(state: AgentState, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_json(), indent=2))


def set_followup(state: AgentState, now: datetime, reason: str, rules: Rules) -> AgentState:
    """Call after a check-in answer that isn't fully resolved yet (spec: 30 min for
    "unexpected plan," 60 min for "continuing current task")."""
    if reason not in FOLLOWUP_REASONS:
        raise ValueError(f"reason must be one of {FOLLOWUP_REASONS}, got {reason!r}")
    minutes = rules.followup_delay_unexpected_minutes if reason == "unexpected" else rules.followup_delay_continuing_minutes
    state.pending_followup_at = now + timedelta(minutes=minutes)
    state.pending_followup_reason = reason
    return state


def clear_followup(state: AgentState) -> AgentState:
    state.pending_followup_at = None
    state.pending_followup_reason = None
    return state


def reminder_intake_expired(pending: PendingReminderIntake, now: datetime, rules: Rules) -> bool:
    """True if too long has passed since the user's last reply in this thread — anchored
    on last_activity_at (a reply resets the clock), not started_at."""
    return (now - pending.last_activity_at) > timedelta(minutes=rules.reminder_intake_timeout_minutes)


@dataclass
class Trigger:
    at: datetime
    reason: str


def compute_next_trigger(
    now: datetime,
    state: AgentState,
    rules: Rules,
    next_block_end: Optional[datetime] = None,
    next_block_start: Optional[datetime] = None,
) -> Trigger:
    """Next trigger = min(next known block start, next known block end, pending
    follow-up, day-boundary floor/retry). There is deliberately no general-purpose hourly
    ping anymore — only a real block starting or ending (or a day-boundary confirmation)
    wakes the scheduler or sends a notification. `safety_backstop_minutes` is not a
    user-facing cadence; it only fires (silently — callers must not notify on it unless
    something's genuinely due) when every other candidate is exhausted, so the scheduler
    always has *something* to wake up for rather than crashing on an empty candidate list
    or never waking again.

    A `block_end` is allowed to already be in the past (that's what "overrun" means — the
    caller is expected to notice and fire immediately) — but it's only ever sourced from
    the caller's own end-time computation, which already filters to `end > now` before
    this is called, so in practice it's never actually stale here either. `block_start` is
    held to the same standard (only ever a genuinely future start). Every OTHER candidate
    is only included if it's still ahead of `now`: nothing in this codebase clears
    pending_followup_at on its own except an actual answer arriving, so a naive min() over
    a stale-but-still-included candidate would keep winning forever — recomputing the same
    past instant as "next trigger" every single tick, which the caller's
    delay-until-next-tick math turns into an immediate refire. This has already happened
    twice independently (once via a stale pending_followup_at, once via evening_floor
    before day-boundary detection existed) — hence the blanket floor at the end too, as a
    backstop against a *third* variant of the same mistake rather than patching candidates
    one at a time forever."""
    candidates: list[Trigger] = []

    if next_block_end is not None:
        candidates.append(Trigger(next_block_end, "block_end"))

    if next_block_start is not None:
        candidates.append(Trigger(next_block_start, "block_start"))

    if state.pending_followup_at is not None and state.pending_followup_at > now:
        candidates.append(Trigger(state.pending_followup_at, "followup"))

    if state.day_start_confirmed_date != now.date():
        morning_floor_dt = datetime.combine(now.date(), rules.morning_checkin_floor)
        if morning_floor_dt > now:
            candidates.append(Trigger(morning_floor_dt, "morning_floor"))
        else:
            candidates.append(
                Trigger(now + timedelta(minutes=rules.floor_confirmation_retry_minutes), "morning_floor_retry")
            )

    if state.day_end_confirmed_date != now.date():
        evening_floor_dt = datetime.combine(now.date(), rules.evening_checkin_floor)
        if evening_floor_dt > now:
            candidates.append(Trigger(evening_floor_dt, "evening_floor"))
        else:
            candidates.append(
                Trigger(now + timedelta(minutes=rules.floor_confirmation_retry_minutes), "evening_floor_retry")
            )

    # Not seeded until the first day-start ever confirms (see AgentState field comment)
    # — no candidate at all before that, so a fresh install doesn't immediately queue a
    # review with zero real usage behind it.
    if state.last_weekly_review_at is not None:
        review_due_dt = state.last_weekly_review_at + timedelta(days=rules.weekly_review_interval_days)
        if review_due_dt > now:
            candidates.append(Trigger(review_due_dt, "weekly_review"))
        else:
            candidates.append(
                Trigger(now + timedelta(minutes=rules.weekly_review_retry_minutes), "weekly_review_retry")
            )

    if not candidates:
        candidates.append(Trigger(now + timedelta(minutes=rules.safety_backstop_minutes), "safety_backstop"))

    trigger = min(candidates, key=lambda t: t.at)

    # Backstop: whatever produced this, never hand back something at/before `now` except
    # the documented block_end/overrun case — anything else risks the tight-loop bug class
    # above recurring in some form we haven't thought of yet.
    if trigger.reason != "block_end" and trigger.at <= now:
        return Trigger(now + timedelta(minutes=rules.safety_backstop_minutes), "safety_backstop")

    return trigger


def is_implicit_day_boundary(now: datetime, state: AgentState, rules: Rules) -> bool:
    """Spec's "Implicit fallback": if the gap since the last successful run exceeds
    day_boundary_gap_hours, treat it as a day boundary regardless of signal (covers a
    dead phone battery or a missed Focus toggle)."""
    if state.last_run_at is None:
        return False
    gap_hours = (now - state.last_run_at).total_seconds() / 3600
    return gap_hours > rules.day_boundary_gap_hours
