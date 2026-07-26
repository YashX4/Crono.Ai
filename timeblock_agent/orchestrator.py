"""Ties every previously-built piece into what the persistent scheduler process actually
does each cycle (see spec: "Day-Start Full Plan" / "Event-Driven Incremental Replan" /
"Day boundary detection"):

- run_scheduled_tick(): called when a trigger fires with no answer attached yet (a
  checkable block starting/ending, a day-boundary floor, or a follow-up delay elapsing).
  Confirms day_start/day_end via the floor-question or implicit-gap fallback when
  appropriate; otherwise sends whatever check-in notification is appropriate — it does
  NOT guess an outcome and replan, since the spec's whole point is confirming instead of
  assuming.
- handle_day_boundary(): called once a floor-confirmation answer is routed back from
  Telegram.
- run_checkin_answer(): called once a real check-in answer comes back. This is what
  actually replans, writes calendar deltas, and logs the resolution.

All three return the next Trigger so the caller (server.py) knows when to wake up again.

Checked in on the agent-authored TASK block's own end time (e.g. "CyberSec App" ending at
17:00), not the bucket container's end time (e.g. "Work" ending at 17:15) — that's the
actually meaningful, answerable checkpoint. Per explicit user preference, check-ins also
cover FIXED commitments (meetings, classes) and already-specific personal blocks (Gym,
Lunch) at their own start/end — everything except bucket containers themselves, which are
represented through the task blocks filling them instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from timeblock_agent import completion_log
from timeblock_agent.block_snapshots import BlockSnapshot, load_snapshots, save_snapshots
from timeblock_agent.bucket_context import load_bucket_context
from timeblock_agent.classify import classify_events, clear_classification_cache
from timeblock_agent.config import Rules, load_rules
from timeblock_agent.day_layout import (
    BucketAdjustment,
    ProposedBlock,
    encode_goal_time_meta,
    overlaps_fixed_event,
    plan_day,
    rank_buckets_by_priority,
    validate_block,
    validate_bucket_adjustment,
)
from timeblock_agent.diff_write import apply_layout, decode_block_meta
from timeblock_agent.eventkit_bridge import CalendarEvent, EventKitBridge, EventKitError
from timeblock_agent.goals import Goal, allocate_goal_sessions, list_goals, pick_goal, record_completion
from timeblock_agent.incremental_replan import replan_incremental
from timeblock_agent.notifier import invalidate_reminder_confirmation, send_notification, send_reminder_confirmation
from timeblock_agent.preferences import append_observation, load_preferences
from timeblock_agent import reminder_intake
from timeblock_agent.reminder_intake import OllamaError
from timeblock_agent.scope import in_calendar_scope, in_reminder_list_scope
from timeblock_agent.state import (
    AgentState,
    PendingReminderIntake,
    Trigger,
    compute_next_trigger,
    is_implicit_day_boundary,
    load_state,
    reminder_intake_expired,
    save_state,
    set_followup,
)
from timeblock_agent.task_progress import (
    PacingInfo,
    TaskProgress,
    is_complete,
    load_task_progress,
    save_task_progress,
)
from timeblock_agent.weekly_review import BucketStats, ManualEdit, generate_observations

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# TIMEBLOCK_RULES_PATH lets a sandboxed test run point at a separate rules file (e.g.
# rules.test.yaml, scoped to dummy test calendars/reminders) without touching the real
# config — see TESTING.md.
RULES_PATH = Path(os.environ.get("TIMEBLOCK_RULES_PATH", str(PROJECT_ROOT / "rules.yaml")))

logger = logging.getLogger("timeblock_agent.orchestrator")

ANSWER_TO_LOG_STATUS = {
    "completed": "completed",
    "running_behind": "still-in-progress",
    "unexpected_plan": "bumped",
}

# Button labels -> minutes, for the day-start "how much goal time today?" prompt. Deliberately
# a plain constant, not a rules.yaml field — these are just the semantics of 4 fixed
# button labels, not something that needs per-user tuning.
GOAL_TIME_MINUTES = {"none": 0, "light": 60, "balanced": 150, "heavy": 240}
GOAL_TIME_BUCKET_TITLE = "Goal Time"


def _day_end(now: datetime) -> datetime:
    return now.replace(hour=23, minute=59, second=59)


def _full_context() -> str:
    """buckets.md + preferences.md combined into the single freeform steering string
    already threaded through classify/day_layout/incremental_replan as `bucket_context`
    — folds the weekly review's observations in everywhere that string is already read,
    with no new parameter needed anywhere downstream (see rules.md, preferences.py)."""
    parts = [p for p in (load_bucket_context(), load_preferences()) if p]
    return "\n\n".join(parts)


def _classify_today_events(
    bridge: EventKitBridge, now: datetime, rules: Rules
) -> tuple[list[CalendarEvent], list[CalendarEvent], list[CalendarEvent]]:
    """One classify_events call, returning (bucket_blocks, fixed_events, specific_events).

    Queries the FULL day (midnight to day-end), not `now` onward — same reasoning as
    _today_agent_blocks/bug #24: run_scheduled_tick only ever sends a check-in
    notification for an external FIXED/FLEXIBLE_SPECIFIC block once it's already overdue
    (`e.end <= now`), so by construction `now` at the moment the user's ANSWER is
    processed (run_checkin_answer, called with no `now` in production — real wall-clock
    time at tap time) is always >= that block's own end. A query starting at `now`
    would then always exclude the very external block being checked in on — silently
    turning every real "was your meeting done / still going?" answer into a no-op
    (triggering_task never found), a different manifestation of the exact same
    now-vs-midnight query-window class of bug already fixed for agent-authored blocks.
    Agent-authored blocks aren't affected by this specific gap (handled separately via
    _today_agent_blocks), but FIXED/FLEXIBLE_SPECIFIC ones have no other source."""
    events = bridge.list_events(now.replace(hour=0, minute=0, second=0, microsecond=0), _day_end(now))
    events = [e for e in events if in_calendar_scope(e.calendar_title, rules)]
    classifications = classify_events(events, rules, _full_context())
    bucket_blocks = [e for e in events if classifications[e.identifier] == "FLEXIBLE_BUCKET"]
    fixed_events = [e for e in events if classifications[e.identifier] == "FIXED"]
    specific_events = [e for e in events if classifications[e.identifier] == "FLEXIBLE_SPECIFIC"]
    return bucket_blocks, fixed_events, specific_events


def _today_agent_blocks(bridge: EventKitBridge, now: datetime, rules: Rules) -> list[CalendarEvent]:
    return bridge.list_events(now.replace(hour=0, minute=0, second=0), _day_end(now), calendar_titles=[rules.agent_calendar])


def _open_reminders(bridge: EventKitBridge, rules: Rules) -> list:
    reminders = bridge.list_reminders(include_completed=False)
    return [r for r in reminders if in_reminder_list_scope(r.list_title, rules)]


def _active_goals() -> list[Goal]:
    try:
        return list_goals()
    except Exception:
        return []


def _goal_candidate(rules: Rules, goals: list[Goal]) -> Optional[Goal]:
    return pick_goal(goals, rules.goal_fill_weights) if goals else None


def _checkable_blocks(
    agent_blocks: list[CalendarEvent],
    fixed_events: list[CalendarEvent],
    specific_events: list[CalendarEvent],
    state: AgentState,
) -> list[CalendarEvent]:
    """Everything worth checking in on at start/end EXCEPT bucket containers themselves:
    agent-authored task blocks, real commitments (meetings/classes), and already-specific
    personal blocks (Gym, Lunch) — per explicit user preference. Bucket containers are
    represented implicitly through the task blocks filling them instead."""
    combined = agent_blocks + fixed_events + specific_events
    return [e for e in combined if e.identifier not in state.resolved_block_ids]


def _compute_next_block_end(
    checkable_blocks: list[CalendarEvent], bucket_blocks: list[CalendarEvent], now: datetime
) -> Optional[datetime]:
    ends = [e.end for e in checkable_blocks if e.end > now] or [e.end for e in bucket_blocks if e.end > now]
    return min(ends, default=None)


def _compute_next_block_start(checkable_blocks: list[CalendarEvent], now: datetime) -> Optional[datetime]:
    starts = [e.start for e in checkable_blocks if e.start > now]
    return min(starts, default=None)


def _source_goal_time(
    bridge: EventKitBridge,
    rules: Rules,
    bucket_blocks: list[CalendarEvent],
    placed_blocks: list[ProposedBlock],
    bucket_roles: dict[str, bool],
    goal_time_minutes: int,
    fixed_events: Optional[list[CalendarEvent]] = None,
    specific_events: Optional[list[CalendarEvent]] = None,
) -> tuple[list[CalendarEvent], Optional[CalendarEvent]]:
    """Sources today's goal-time budget as ONE contiguous block from exactly one bucket
    (see rules.md §2 — goal time is conceptually Work's own budget, not a claim on
    leisure time). Real leftover slack per bucket is only knowable now, after plan_day's
    actual reminder placements (`placed_blocks`) are known — this used to be a blind
    pre-guess (rank every bucket, take the lowest), which is what this replaces.

    Preference order: (1) a `goal_time_eligible` bucket whose own real leftover slack
    alone covers the full budget — no boundary change, just using room already going
    spare; among several, whichever has the most. (2) If none qualifies, shrink a
    NON-eligible bucket (e.g. HobbyMaxxing) by the full amount instead — this is the one
    case goal time is allowed to touch leisure time, only when work-type buckets alone
    can't cover it. Returns (possibly updated bucket_blocks, the new "Goal Time"
    CalendarEvent or None if nothing could be safely sourced — never guesses)."""
    if goal_time_minutes <= 0:
        return bucket_blocks, None

    fixed_events = fixed_events or []

    used_minutes: dict[str, float] = {}
    for b in placed_blocks:
        used_minutes[b.bucket_event_id] = used_minutes.get(b.bucket_event_id, 0) + (b.end - b.start).total_seconds() / 60

    def _fixed_minutes_within(bucket: CalendarEvent) -> float:
        total = 0.0
        for fe in fixed_events:
            overlap_start, overlap_end = max(bucket.start, fe.start), min(bucket.end, fe.end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 60
        return total

    def slack(bucket: CalendarEvent) -> float:
        # A nested FIXED event (e.g. a meeting sitting inside this bucket, same as Test
        # Meeting inside Test Work) occupies real time too, same as a placed task block
        # does — omitting it here previously overstated how much room a bucket genuinely
        # had spare, and could pick a bucket (or a placement within one) that actually
        # overlapped a real commitment (see the fixed_events overlap check below, which
        # this reduces the odds of ever needing to trigger).
        window_minutes = (bucket.end - bucket.start).total_seconds() / 60
        return window_minutes - used_minutes.get(bucket.identifier, 0) - _fixed_minutes_within(bucket)

    eligible = [b for b in bucket_blocks if bucket_roles.get(b.identifier, False)]
    covering = [(b, slack(b)) for b in eligible if slack(b) >= goal_time_minutes]

    donor: Optional[CalendarEvent] = None
    shrink_needed = False
    if covering:
        donor, _ = max(covering, key=lambda pair: pair[1])
    else:
        non_eligible = [b for b in bucket_blocks if not bucket_roles.get(b.identifier, False)]
        if not non_eligible:
            logger.info("No eligible bucket has enough slack and no fallback bucket exists — skipping goal time")
            return bucket_blocks, None
        try:
            ranked_ids = rank_buckets_by_priority(non_eligible, _full_context())
        except Exception:
            logger.exception("Bucket priority ranking failed — skipping goal-time carve-out")
            return bucket_blocks, None
        by_id = {b.identifier: b for b in non_eligible}
        donor = by_id.get(ranked_ids[0]) if ranked_ids else None
        shrink_needed = True

    if donor is None:
        return bucket_blocks, None

    if shrink_needed:
        available = (donor.end - donor.start).total_seconds() / 60 - rules.min_block_minutes
        carve_minutes = min(goal_time_minutes, available)
        if carve_minutes < rules.min_block_minutes:
            logger.info(
                "Not enough room in %r to source goal time (%.0f min available) — skipping",
                donor.title, available,
            )
            return bucket_blocks, None
        new_donor_end = donor.end - timedelta(minutes=carve_minutes)
        goal_start, goal_end = new_donor_end, donor.end
    else:
        # Natural slack: append after whatever's already placed in the donor bucket —
        # a simplification (leftover slack is assumed to trail the last placed block,
        # not sit in a mid-window gap), reasonable since plan_day fills sequentially.
        donor_blocks_end = max(
            (b.end for b in placed_blocks if b.bucket_event_id == donor.identifier), default=donor.start
        )
        goal_start = donor_blocks_end
        goal_end = min(donor_blocks_end + timedelta(minutes=goal_time_minutes), donor.end)
        goal_start = goal_end - timedelta(minutes=goal_time_minutes)

    # Final safety net, checked BEFORE any real write happens: neither branch above is
    # aware of a fixed event that might sit inside the chosen window (the "leftover
    # trails the last placed block" simplification doesn't know about one interspersed
    # there) — every other block-placement path in this codebase checks this, so Goal
    # Time shouldn't be the one exception. Bail rather than try to cleverly route around
    # it — goal time is a nice-to-have, never worth risking a real commitment over. This
    # must happen before the shrink branch's bridge.update_event() below — checking after
    # would leave a donor bucket shrunk for real with no Goal Time created to account for
    # the lost time.
    if overlaps_fixed_event(goal_start, goal_end, fixed_events):
        logger.info(
            "Computed goal-time window %s-%s would overlap a real fixed event — skipping",
            goal_start.strftime("%H:%M"), goal_end.strftime("%H:%M"),
        )
        return bucket_blocks, None
    # Same check against everything else already on the calendar today (other buckets,
    # FLEXIBLE_SPECIFIC blocks like Gym) — not just real FIXED commitments. The donor
    # itself is excluded: its own original span always contains the carved window by
    # construction, so including it would always (wrongly) flag an overlap.
    other_events = [b for b in ((specific_events or []) + bucket_blocks) if b.identifier != donor.identifier]
    if overlaps_fixed_event(goal_start, goal_end, other_events):
        logger.info(
            "Computed goal-time window %s-%s would overlap another already-scheduled block — skipping",
            goal_start.strftime("%H:%M"), goal_end.strftime("%H:%M"),
        )
        return bucket_blocks, None

    if shrink_needed:
        bridge.update_event(donor.identifier, end=new_donor_end)
        logger.info(
            "Shrunk %r to %s-%s to source %d min of goal time",
            donor.title, donor.start.strftime("%H:%M"), new_donor_end.strftime("%H:%M"), carve_minutes,
        )
        bucket_blocks = [replace(b, end=new_donor_end) if b.identifier == donor.identifier else b for b in bucket_blocks]

    goal_time_id = bridge.create_event(
        GOAL_TIME_BUCKET_TITLE, goal_start, goal_end,
        calendar_title=donor.calendar_title, notes=encode_goal_time_meta(donor.identifier),
    )
    goal_time_bucket = replace(
        donor, identifier=goal_time_id, title=GOAL_TIME_BUCKET_TITLE, start=goal_start, end=goal_end,
        notes=encode_goal_time_meta(donor.identifier),
    )
    logger.info(
        "Created %r %s-%s (sourced from %r)",
        GOAL_TIME_BUCKET_TITLE, goal_start.strftime("%H:%M"), goal_end.strftime("%H:%M"), donor.title,
    )
    return bucket_blocks + [goal_time_bucket], goal_time_bucket


def _build_goal_session_blocks(
    goal_time_bucket: CalendarEvent, rules: Rules, goals: list[Goal], goal_time_minutes: int, now: datetime,
    fixed_events: Optional[list[CalendarEvent]] = None,
) -> list[ProposedBlock]:
    """Splits the day's goal-time budget across one or more goals (see
    goals.allocate_goal_sessions — no single goal gets more than
    rules.goal_fill_weights.max_minutes_per_goal), laid out as consecutive segments
    filling goal_time_bucket's own window exactly. Pure Python placement — the window
    and the minutes-per-session are both already fully known, nothing left to ask a
    model to decide."""
    sessions = allocate_goal_sessions(
        goals, rules.goal_fill_weights, goal_time_minutes, rules.goal_fill_weights.max_minutes_per_goal
    )
    buckets_by_id = {goal_time_bucket.identifier: goal_time_bucket}
    blocks = []
    session_start = goal_time_bucket.start
    for session in sessions:
        session_end = session_start + timedelta(minutes=session.minutes)
        block = ProposedBlock(
            bucket_event_id=goal_time_bucket.identifier,
            title=session.goal.next_action,
            start=session_start,
            end=session_end,
            source="goal",
            source_id="goal",
        )
        if validate_block(block, buckets_by_id, rules, now, goal_candidate_given=True, fixed_events=fixed_events):
            blocks.append(block)
        session_start = session_end
    return blocks


def _compute_task_pacing(
    bridge: EventKitBridge,
    rules: Rules,
    reminders: list,
    task_progress: dict[str, TaskProgress],
    bucket_context: str,
    now: datetime,
) -> dict[str, PacingInfo]:
    """Today's pacing target for any reminder already tracked across days
    (`task_progress`) that also carries a due date, or a fresh due-dated reminder that
    hasn't been sized yet. Only runs the extra week-ahead classify call if at least one
    candidate exists — zero added cost on an ordinary day (see rules.md §2).

    A task's `total_minutes` only exists once plan_day has estimated it once already —
    there's nothing to pace on the very first day a big task is seen; that day just
    behaves like the existing single-day best-effort fit, and the estimate it reports
    is what makes pacing possible starting the next day.

    Capacity is a deliberate approximation: total FLEXIBLE_BUCKET minutes anywhere in
    scope between now and each reminder's own due date, not scoped to one specific
    calendar (today's actual bucket match isn't known yet — that's plan_day's own job,
    which hasn't run when this is called) and not netted against future FIXED overlaps
    or hypothetical competing reminders. Good enough to flag "at risk" and front-load
    accordingly; tomorrow's fresh run corrects course regardless. Raises on failure —
    caller should catch and fall back to plain best-effort sizing for the cycle."""
    today = now.date()
    candidates: dict[str, date] = {}
    for r in reminders:
        tp = task_progress.get(r.identifier)
        if tp is not None and tp.due_date is not None:
            candidates[r.identifier] = tp.due_date
        elif r.due_date is not None:
            candidates[r.identifier] = r.due_date.date()

    if not candidates:
        return {}

    max_due = min(max(candidates.values()), today + timedelta(days=30))
    future_events = bridge.list_events(now, datetime.combine(max_due, datetime.max.time()))
    future_events = [e for e in future_events if in_calendar_scope(e.calendar_title, rules)]
    classifications = classify_events(future_events, rules, bucket_context)
    future_bucket_events = [e for e in future_events if classifications[e.identifier] == "FLEXIBLE_BUCKET"]

    pacing: dict[str, PacingInfo] = {}
    for reminder_id, due_date in candidates.items():
        tp = task_progress.get(reminder_id)
        if tp is None:
            continue  # day 1 for this task — no total estimate yet, nothing to pace
        remaining = max(tp.total_minutes - tp.completed_minutes, 0)
        if remaining <= 0:
            continue

        overdue = due_date < today
        days_remaining = max((due_date - today).days + 1, 1)
        capacity = sum(
            (e.end - e.start).total_seconds() / 60 for e in future_bucket_events if e.start.date() <= due_date
        )
        at_risk = capacity < remaining

        even_split = -(-remaining // days_remaining)  # ceil
        if overdue:
            today_target = remaining
        elif at_risk:
            today_target = min(remaining, even_split * 2)
        else:
            today_target = even_split

        pacing[reminder_id] = PacingInfo(
            today_target_minutes=max(int(today_target), 1),
            remaining_minutes=remaining,
            days_remaining=days_remaining,
            at_risk=at_risk,
            overdue=overdue,
        )
    return pacing


def run_day_start_pass(
    bridge: EventKitBridge,
    rules: Rules,
    now: datetime,
    bucket_blocks: list[CalendarEvent],
    fixed_events: list[CalendarEvent],
    goal_time_minutes: int = 0,
    specific_events: Optional[list[CalendarEvent]] = None,
) -> tuple[list[str], int]:
    """Returns (at_risk_titles, actual_goal_time_minutes_sourced) — titles of any
    reminder behind pace for its own deadline, and however much goal-time budget actually
    got sourced (see _source_goal_time's shrink-fallback: the donor bucket may not always
    have enough room to cover the full request — this used to be silently under-delivered
    with no signal anywhere). Both fold into the day-start notification text (no new
    notification kind — see rules.md §2). `specific_events` (FLEXIBLE_SPECIFIC — Gym,
    Lunch, etc.) protect against a bucket extension silently overlapping one — caught
    live: an extended bucket ran straight through a neighboring specific block."""
    reminders = _open_reminders(bridge, rules)
    goals = _active_goals()
    goal_candidate = _goal_candidate(rules, goals)
    bucket_context = _full_context()

    task_progress = load_task_progress()
    open_reminder_ids = {r.identifier for r in reminders}
    for stale_id in [rid for rid in task_progress if rid not in open_reminder_ids]:
        del task_progress[stale_id]
        logger.info("Dropped stale task-progress entry for %s (no longer an open reminder)", stale_id)

    try:
        task_pacing = _compute_task_pacing(bridge, rules, reminders, task_progress, bucket_context, now)
    except Exception:
        logger.exception("Task pacing computation failed — falling back to best-effort sizing")
        task_pacing = {}

    result = plan_day(
        now, bucket_blocks, reminders, rules, goal_candidate,
        fixed_events=fixed_events, bucket_context=bucket_context, task_pacing=task_pacing,
        specific_events=specific_events,
    )
    logger.info(
        "plan_day: %d proposed block(s), %d unscheduled reminder(s), %d bucket adjustment(s)",
        len(result.blocks), len(result.unscheduled_reminder_ids), len(result.bucket_adjustments),
    )
    bucket_blocks = _apply_bucket_adjustments(bridge, bucket_blocks, result.bucket_adjustments)

    scheduled_minutes: dict[str, float] = {}
    for b in result.blocks:
        if b.source == "reminder":
            scheduled_minutes[b.source_id] = scheduled_minutes.get(b.source_id, 0) + (b.end - b.start).total_seconds() / 60

    at_risk_titles: list[str] = []
    reminders_by_id = {r.identifier: r for r in reminders}
    for est in result.task_estimates:
        if not est.due_date:
            continue  # no deadline signal at all — not tracked, same as today's existing behavior
        # datetime.fromisoformat (not date.fromisoformat) tolerates the model echoing back
        # a full datetime string instead of a bare date — seen live: due_date was sent as
        # a plain date but came back as "...T00:00:00" anyway.
        due = datetime.fromisoformat(est.due_date).date()
        if est.realistic_total_minutes <= scheduled_minutes.get(est.reminder_id, 0):
            continue  # entirely covered today — no need to track across days
        existing = task_progress.get(est.reminder_id)
        if existing is None:
            task_progress[est.reminder_id] = TaskProgress(
                reminder_id=est.reminder_id, total_minutes=est.realistic_total_minutes,
                completed_minutes=0, due_date=due, first_seen=now.date(),
            )
        else:
            existing.due_date = due  # keep in sync in case a free-text parse changes it
        pacing = task_pacing.get(est.reminder_id)
        if pacing is not None and pacing.at_risk:
            reminder = reminders_by_id.get(est.reminder_id)
            at_risk_titles.append(reminder.title if reminder else est.reminder_id)

    save_task_progress(task_progress)

    bucket_blocks, goal_time_bucket = _source_goal_time(
        bridge, rules, bucket_blocks, result.blocks, result.bucket_roles, goal_time_minutes,
        fixed_events, specific_events,
    )
    actual_goal_minutes = (
        int((goal_time_bucket.end - goal_time_bucket.start).total_seconds() / 60) if goal_time_bucket else 0
    )
    if goal_time_minutes > 0 and actual_goal_minutes < goal_time_minutes:
        logger.warning(
            "Goal time shortfall: requested %d min, only sourced %d — not enough spare "
            "capacity in the chosen donor bucket today",
            goal_time_minutes, actual_goal_minutes,
        )

    blocks = list(result.blocks)
    if goal_time_bucket is not None:
        blocks += _build_goal_session_blocks(goal_time_bucket, rules, goals, goal_time_minutes, now, fixed_events)

    # Full day (via _today_agent_blocks), not `now` onward — same reasoning as
    # _write_incremental_result: a query starting at `now` would miss any pre-existing
    # agent block whose own end already passed relative to current time (e.g. a retry
    # after a prior day-start attempt partially wrote blocks earlier that day), silently
    # duplicating it instead of updating/reconciling it.
    existing = _today_agent_blocks(bridge, now, rules)
    summary = apply_layout(bridge, blocks, existing, rules.agent_calendar, written_at=now)
    logger.info(
        "apply_layout: created=%d updated=%d deleted=%d unchanged=%d",
        len(summary.created), len(summary.updated), len(summary.deleted), len(summary.unchanged),
    )
    return at_risk_titles, actual_goal_minutes


def _confirm_day_start(
    bridge: EventKitBridge, rules: Rules, state: AgentState, now: datetime, goal_time_minutes: int = 0
) -> tuple[list[CalendarEvent], list[CalendarEvent], list[CalendarEvent]]:
    state.day_start_confirmed_date = now.date()
    state.resolved_block_ids = set()
    state.started_block_ids = set()
    if state.last_weekly_review_at is None:
        # First real day-start ever confirmed — reviews are due `weekly_review_interval_days`
        # from here, not from install time (see AgentState field comment / state.py).
        state.last_weekly_review_at = now
    clear_classification_cache()  # clean slate each day — see classify.py's cache docstring
    bucket_blocks, fixed_events, specific_events = _classify_today_events(bridge, now, rules)
    at_risk_titles, actual_goal_minutes = run_day_start_pass(
        bridge, rules, now, bucket_blocks, fixed_events, goal_time_minutes, specific_events
    )
    text = "Full plan written for the rest of today."
    if at_risk_titles:
        text += f" Behind pace for its deadline: {', '.join(at_risk_titles)}."
    if goal_time_minutes > 0 and actual_goal_minutes < goal_time_minutes:
        text += f" Only found {actual_goal_minutes} of {goal_time_minutes} min of goal time today."
    send_notification("day_start", "Day started", text)
    return bucket_blocks, fixed_events, specific_events


def _aggregate_bucket_stats(log_entries: list[dict]) -> list[BucketStats]:
    """Per-bucket completed/bumped/still-in-progress counts across the reviewed window
    (see completion_log.read_logs_between) — the raw material weekly_review.generate_observations
    turns into prose."""
    counts: dict[str, dict[str, int]] = {}
    for entry in log_entries:
        bucket_counts = counts.setdefault(entry["bucket"], {"completed": 0, "bumped": 0, "still-in-progress": 0})
        bucket_counts[entry["status"]] += 1
    return [
        BucketStats(
            bucket=bucket, completed=c["completed"], bumped=c["bumped"], still_in_progress=c["still-in-progress"]
        )
        for bucket, c in counts.items()
    ]


def _detect_manual_edits(bridge: EventKitBridge, rules: Rules, now: datetime) -> list[ManualEdit]:
    """Compares every CURRENTLY STORED block_snapshots entry against what's actually in
    the agent calendar today — one list_events call spanning the union of those
    snapshots' own start/end times, not one call per identifier. A snapshot with no
    matching current event is a manual deletion (a system-initiated delete already removed
    its snapshot via diff_write's remove_snapshot, so this can only be a manual one, see
    block_snapshots.py); a title mismatch is a manual retitle; a start/end mismatch alone
    is a manual resize. An exact match is dropped — nothing worth reporting.

    Deliberately examines every remaining snapshot, not just ones written since the last
    review — a real bug, caught live: filtering to `written_at >= since` meant a snapshot
    written *before* the current review's `since` (but not yet purged, e.g. from a block
    created the same day an earlier review happened to fire) would never be examined by
    ANY future review either, since `since` only ever moves forward — the edit would go
    undetected forever. Now that `_purge_reviewed_snapshots` only removes snapshots whose
    own block has aged into a past day (not based on review timing), "still in the
    store" and "still worth checking" are the same thing, so there's no separate window
    to apply here."""
    snapshots = load_snapshots()
    in_window = list(snapshots.values())
    if not in_window:
        return []

    window_start = min(s.start for s in in_window)
    window_end = max(s.end for s in in_window) + timedelta(days=1)
    current_by_id = {
        e.identifier: e for e in bridge.list_events(window_start, window_end, calendar_titles=[rules.agent_calendar])
    }

    edits: list[ManualEdit] = []
    for snap in in_window:
        current = current_by_id.get(snap.identifier)
        if current is None:
            edits.append(ManualEdit(kind="deleted", snapshot_title=snap.title, snapshot_start=snap.start, snapshot_end=snap.end))
        elif current.title != snap.title:
            edits.append(ManualEdit(
                kind="retitled", snapshot_title=snap.title, snapshot_start=snap.start, snapshot_end=snap.end,
                current_title=current.title, current_start=current.start, current_end=current.end,
            ))
        elif current.start != snap.start or current.end != snap.end:
            edits.append(ManualEdit(
                kind="resized", snapshot_title=snap.title, snapshot_start=snap.start, snapshot_end=snap.end,
                current_title=current.title, current_start=current.start, current_end=current.end,
            ))
    return edits


def _purge_reviewed_snapshots(now: datetime) -> None:
    """Drops snapshots for blocks whose own scheduled time is safely in the past (before
    today) — so the store stays bounded rather than growing forever.

    Deliberately NOT based on written_at falling in the reviewed [since, now) window —
    that was a real bug, caught live: a block created earlier the same day a review
    happens to fire gets its snapshot purged by THAT review, before there's ever been a
    chance to manually edit it; a hand-edit made afterward (still that same day) then has
    nothing to compare against and silently goes undetected forever, at every future
    review too, since the snapshot is simply gone. Purging by the block's own date
    instead guarantees a same-day block survives at least until the next calendar day,
    giving a same-day manual edit a fair chance to be caught by whatever review comes
    after that."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    snapshots = load_snapshots()
    remaining = {identifier: s for identifier, s in snapshots.items() if s.end >= today_start}
    if len(remaining) != len(snapshots):
        save_snapshots(remaining)


def run_weekly_review(bridge: EventKitBridge, rules: Rules, state: AgentState, now: datetime) -> None:
    """Aggregates completion-log patterns and detects silent manual calendar edits since
    the last review, turns them into a few sentences via weekly_review.generate_observations
    (the one Claude call in this module that's descriptive, not a consequential scheduling
    decision — see that module's docstring), appends them to preferences.md, purges the
    reviewed snapshot window, and sends one plain FYI notification.

    Only advances state.last_weekly_review_at on success — a failure here leaves it
    untouched so compute_next_trigger's weekly_review_retry candidate fires again soon
    instead of silently skipping a whole interval's worth of review (same
    never-lose-a-cycle-to-a-swallowed-exception discipline as the rest of this module)."""
    since = state.last_weekly_review_at or (now - timedelta(days=rules.weekly_review_interval_days))

    try:
        log_entries = completion_log.read_logs_between(since.date(), now.date())
        bucket_stats = _aggregate_bucket_stats(log_entries)
        manual_edits = _detect_manual_edits(bridge, rules, now)
        observations = generate_observations(bucket_stats, manual_edits)
        if observations:
            append_observation(observations, when=now)
        _purge_reviewed_snapshots(now)
    except Exception:
        logger.exception("Weekly review failed — will retry")
        return

    state.last_weekly_review_at = now
    count = 1 if observations else 0
    send_notification(
        "weekly_review", "Weekly review done",
        f"{count} new observation{'s' if count != 1 else ''} logged." if count else "Nothing notable this week.",
    )


def run_scheduled_tick(
    bridge: EventKitBridge, state_path: Optional[Path] = None, now: Optional[datetime] = None
) -> Trigger:
    """A trigger fired with no answer attached (a checkable block starting/ending, a
    day-boundary floor, or a follow-up delay elapsing). Confirms day_start/day_end when
    appropriate; otherwise sends whatever check-in notification is appropriate — never
    assumes an outcome and replans on its own.

    `now` is optional and defaults to real time — same `when=None` precedent already used
    by `goals.record_completion`/`preferences.append_observation` — so a caller driving a
    fully offline/fake-EventKit test can inject a synthetic clock instead of waiting for
    real wall-clock time to reach whatever moment it needs to test (see fake_sandbox/)."""
    rules = load_rules(RULES_PATH)
    state = load_state(state_path) if state_path else load_state()
    now = now or datetime.now()

    # An elapsed follow-up that nobody answered is "used up" here — the overdue-block
    # detection below re-asks anyway, based on the block's own end time, not this.
    if state.pending_followup_at is not None and state.pending_followup_at <= now:
        state.pending_followup_at = None
        state.pending_followup_reason = None

    # Weekly review: checked directly against state/rules/now (not the trigger reason
    # string that woke this tick — this function never receives one, see module
    # docstring), same as the morning/evening floor checks further down. Runs before the
    # day-boundary branches below so it fires on whichever tick happens to hit it,
    # regardless of which of those branches' early-return this tick would otherwise take.
    if state.last_weekly_review_at is not None:
        review_due_dt = state.last_weekly_review_at + timedelta(days=rules.weekly_review_interval_days)
        if now >= review_due_dt:
            run_weekly_review(bridge, rules, state, now)

    # Implicit fallback: a big enough gap since the last run means we treat this as a
    # fresh day boundary regardless of any confirmation signal (dead battery, missed
    # Focus toggle, etc.) — covers the case day_start was never properly confirmed at all.
    if is_implicit_day_boundary(now, state, rules) and state.day_start_confirmed_date != now.date():
        logger.info(
            "Implicit day boundary (gap > %.1fh since last run) — force-confirming day_start",
            rules.day_boundary_gap_hours,
        )
        state.day_end_confirmed_date = None
        bucket_blocks, fixed_events, specific_events = _confirm_day_start(bridge, rules, state, now)
        state.last_run_at = now
        agent_blocks = _today_agent_blocks(bridge, now, rules)
        checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)
        trigger = compute_next_trigger(
            now, state, rules,
            _compute_next_block_end(checkable, bucket_blocks, now),
            _compute_next_block_start(checkable, now),
        )
        save_state(state, state_path) if state_path else save_state(state)
        return trigger

    # Day not yet confirmed started: only the floor confirmation question matters — skip
    # everything else (including the classify_events call) until it's answered, since we
    # don't even know yet whether the user is awake for any of today's blocks to matter.
    if state.day_start_confirmed_date != now.date():
        if state.day_start_awaiting_goal_time:
            pass  # already said "yes" and got asked about goal time — wait for that
            # answer instead of re-asking "are you up?" again.
        else:
            morning_floor_dt = datetime.combine(now.date(), rules.morning_checkin_floor)
            if now >= morning_floor_dt:
                send_notification("day_boundary_start", "Are you up?", "Tap yes to start planning today.")
        state.last_run_at = now
        trigger = compute_next_trigger(now, state, rules)
        save_state(state, state_path) if state_path else save_state(state)
        return trigger

    # Once day_end is confirmed, the "Day ended... check-ins are done until tomorrow"
    # notification is a literal promise, not just a vibe — caught live testing Scenario
    # 10: a still-in-progress FIXED meeting's own overdue check-in fired 7 seconds after
    # that exact message, since this whole block (and the block/start trigger candidates
    # that follow) previously ran unconditionally regardless of day_end_confirmed_date.
    # Gating it here means the promise is actually kept, and — a free side effect — skips
    # a classify_events call for the rest of the day once there's nothing left to check.
    day_over = state.day_end_confirmed_date == now.date()

    next_block_end = None
    next_block_start = None
    if not day_over:
        bucket_blocks, fixed_events, specific_events = _classify_today_events(bridge, now, rules)
        agent_blocks = _today_agent_blocks(bridge, now, rules)
        checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)

        overdue = [e for e in checkable if e.end <= now]
        just_starting = [e for e in checkable if e.start <= now and e.identifier not in state.started_block_ids]

        if overdue:
            block = min(overdue, key=lambda e: e.end)
            send_notification(
                "checkin",
                f"{block.title!r} check-in",
                f"Was supposed to end at {block.end:%H:%M} — done, still going, or something unexpected come up?",
                block_id=block.identifier,
            )
        elif just_starting:
            block = min(just_starting, key=lambda e: e.start)
            send_notification("block_start", f"{block.title!r} starting", f"Starting now, runs until {block.end:%H:%M}.")
            # Mark every currently-due start as announced, not just the one we notified about,
            # so simultaneous starts don't each re-trigger this on the next tick.
            for e in just_starting:
                state.started_block_ids.add(e.identifier)
        # else: nothing due this tick (e.g. the silent safety backstop fired) — no-op.

        # Evening floor: layered on top of whatever else this tick did, since the day's
        # already in progress and both can coexist (unlike morning, before anything's begun).
        evening_floor_dt = datetime.combine(now.date(), rules.evening_checkin_floor)
        if now >= evening_floor_dt:
            send_notification(
                "day_boundary_end", "Done for the day?", "Tap yes to wrap up, or still going to keep checking in."
            )

        agent_blocks = _today_agent_blocks(bridge, now, rules)  # fresh read — picks up any writes above
        checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)
        next_block_end = _compute_next_block_end(checkable, bucket_blocks, now)
        next_block_start = _compute_next_block_start(checkable, now)

    state.last_run_at = now
    trigger = compute_next_trigger(now, state, rules, next_block_end, next_block_start)
    save_state(state, state_path) if state_path else save_state(state)
    return trigger


def handle_day_boundary(
    bridge: EventKitBridge, event: str, answer: str,
    state_path: Optional[Path] = None, now: Optional[datetime] = None,
) -> Trigger:
    """event: 'day_start' | 'day_end'. answer: 'yes' | 'no'. Called once a
    floor-confirmation button tap is routed back via Telegram.

    On a "yes" for day_start, day_start isn't confirmed immediately — a goal-time budget
    question goes out first (see handle_goal_time_answer), so the day only gets planned
    once that's answered.

    `now` optional, defaults to real time — see run_scheduled_tick's docstring."""
    if event not in ("day_start", "day_end"):
        raise ValueError(f"event must be 'day_start' or 'day_end', got {event!r}")

    rules = load_rules(RULES_PATH)
    state = load_state(state_path) if state_path else load_state()
    now = now or datetime.now()

    if event == "day_start" and answer == "yes":
        state.day_start_awaiting_goal_time = True
        send_notification(
            "goal_time_prompt", "How much goal time today?",
            "None / Light ~1h / Balanced ~2-3h / Heavy ~4h+",
        )
    elif event == "day_end" and answer == "yes":
        state.day_end_confirmed_date = now.date()
        send_notification("day_end", "Day ended", "Nice work today — check-ins are done until tomorrow.")
    # answer == "no" (not yet / still going): no-op, just recompute the trigger below.

    state.last_run_at = now
    next_block_end = next_block_start = None
    if state.day_start_confirmed_date == now.date():
        bucket_blocks, fixed_events, specific_events = _classify_today_events(bridge, now, rules)
        agent_blocks = _today_agent_blocks(bridge, now, rules)
        checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)
        next_block_end = _compute_next_block_end(checkable, bucket_blocks, now)
        next_block_start = _compute_next_block_start(checkable, now)
    trigger = compute_next_trigger(now, state, rules, next_block_end, next_block_start)
    save_state(state, state_path) if state_path else save_state(state)
    return trigger


def handle_goal_time_answer(
    bridge: EventKitBridge, level: str,
    state_path: Optional[Path] = None, now: Optional[datetime] = None,
) -> Trigger:
    """level: one of GOAL_TIME_MINUTES's keys ('none' | 'light' | 'balanced' | 'heavy').
    Called once the goal-time budget prompt (sent by handle_day_boundary on a Telegram
    "yes, I'm up") gets answered — this is what actually confirms day_start and plans the
    day, carrying the chosen budget into _confirm_day_start.

    `now` optional, defaults to real time — see run_scheduled_tick's docstring."""
    rules = load_rules(RULES_PATH)
    state = load_state(state_path) if state_path else load_state()
    now = now or datetime.now()

    state.day_start_awaiting_goal_time = False
    goal_time_minutes = GOAL_TIME_MINUTES.get(level, 0)
    bucket_blocks, fixed_events, specific_events = _confirm_day_start(bridge, rules, state, now, goal_time_minutes)

    state.last_run_at = now
    agent_blocks = _today_agent_blocks(bridge, now, rules)
    checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)
    trigger = compute_next_trigger(
        now, state, rules,
        _compute_next_block_end(checkable, bucket_blocks, now),
        _compute_next_block_start(checkable, now),
    )
    save_state(state, state_path) if state_path else save_state(state)
    return trigger


def _describe_proposed_reminder(title: str, notes: Optional[str], due_date: Optional[datetime]) -> str:
    parts = [f"Proposed: {title}"]
    if notes:
        parts.append(f"notes: {notes}")
    parts.append(f"due: {due_date.isoformat()}" if due_date else "no due date")
    return " — ".join(parts)


def handle_reminder_message(
    bridge: EventKitBridge, text: str,
    state_path: Optional[Path] = None, now: Optional[datetime] = None,
) -> None:
    """One incoming plain-text Telegram message in a conversational-reminder-intake
    exchange (see rules.md §10, reminder_intake.py). Unlike every other handler in this
    module, this is orthogonal to scheduling — it never returns a Trigger and callers
    (server.py) must not reschedule off of it.

    The thread stays open even past a "ready" draft — a further reply is fed back in as
    feedback to revise the draft (a fresh confirmation, invalidating the stale one), not
    treated as an unrelated new request. It only actually closes on an explicit
    Yes/Cancel tap (see handle_reminder_confirmation) or a plain-language cancel here.

    A genuine topic shift (see reminder_intake.py's `same_topic`) is handled by starting
    a brand new thread from just the latest message, WITHOUT touching the old one at all
    — its confirmation, if already shown, is left exactly as it was (still tappable
    later) rather than invalidated, since the user never said to drop it, just moved on
    to something else for now. This is what actually prevents two unrelated in-flight
    reminders (e.g. an unconfirmed dentist booking, then a restaurant ask two minutes
    later) from getting blended together.

    `now` optional, defaults to real time — see run_scheduled_tick's docstring."""
    rules = load_rules(RULES_PATH)
    state = load_state(state_path) if state_path else load_state()
    now = now or datetime.now()

    pending = state.pending_reminder_intake
    if pending is not None and reminder_intake_expired(pending, now, rules):
        logger.info("reminder_intake: prior thread expired — starting fresh")
        pending = None

    if pending is not None:
        messages = pending.messages + [{"role": "user", "content": text}]
        started_at = pending.started_at
        prior_confirm_token = pending.active_confirm_token
    else:
        messages = [{"role": "user", "content": text}]
        started_at = now
        prior_confirm_token = None

    try:
        result = reminder_intake.resolve(messages, now)
    except OllamaError:
        logger.exception("reminder_intake: resolve() failed — preserving thread for retry")
        state.pending_reminder_intake = PendingReminderIntake(
            messages=messages, started_at=started_at, last_activity_at=now,
            active_confirm_token=prior_confirm_token,
        )
        send_notification(
            "reminder", "Hmm, having trouble right now",
            "Try sending that again in a moment.",
        )
        save_state(state, state_path) if state_path else save_state(state)
        return

    if not result.same_topic and pending is not None:
        logger.info("reminder_intake: topic shift detected — starting a fresh thread, old draft left untouched")
        messages = [{"role": "user", "content": text}]
        started_at = now
        prior_confirm_token = None  # deliberately NOT invalidated — the old one, if shown, stays valid

    if result.action == "clarify":
        invalidate_reminder_confirmation(prior_confirm_token)
        messages = messages + [{"role": "assistant", "content": result.clarifying_question}]
        state.pending_reminder_intake = PendingReminderIntake(
            messages=messages, started_at=started_at, last_activity_at=now,
            active_confirm_token=None,
        )
        send_notification("reminder", "Quick question", result.clarifying_question)
    elif result.action == "cancel":
        invalidate_reminder_confirmation(prior_confirm_token)
        state.pending_reminder_intake = None
        send_notification("reminder", "Okay", "Dropped it — nothing added.")
    else:  # "ready" — possibly a revision of an already-shown draft
        proposal_note = _describe_proposed_reminder(result.title, result.notes, result.due_date)
        messages = messages + [{"role": "assistant", "content": proposal_note}]
        new_token = send_reminder_confirmation(
            result.title, result.notes, result.due_date, previous_token=prior_confirm_token,
        )
        state.pending_reminder_intake = PendingReminderIntake(
            messages=messages, started_at=started_at, last_activity_at=now,
            active_confirm_token=new_token,
        )

    save_state(state, state_path) if state_path else save_state(state)


def handle_reminder_confirmation(
    bridge: EventKitBridge, answer: str, title: str,
    notes: Optional[str] = None, due_date: Optional[datetime] = None,
    state_path: Optional[Path] = None, now: Optional[datetime] = None,
) -> None:
    """The Yes/Cancel tap that resolves a reminder_confirm callback (see server.py). Takes
    the parsed payload as arguments (mirroring run_checkin_answer's block_id) rather than
    reading it back off state, since the token/context system (telegram_notifier.py) is
    what already carried it here. Orthogonal to scheduling — no Trigger, no reschedule.

    An explicit tap definitively ends the conversation — clears pending_reminder_intake
    regardless of yes/cancel/failure, since the thread now stays open through the
    confirmation step itself (see handle_reminder_message) specifically to allow revision
    by text; once the user acts via the real buttons, there's nothing left to revise."""
    state = load_state(state_path) if state_path else load_state()
    state.pending_reminder_intake = None
    save_state(state, state_path) if state_path else save_state(state)

    if answer == "cancel":
        send_notification("reminder", "Okay", "Not adding it.")
        return

    rules = load_rules(RULES_PATH)
    try:
        bridge.create_reminder(title, rules.reminder_intake_list, due_date=due_date, notes=notes)
    except EventKitError:
        logger.exception("reminder_intake: create_reminder failed")
        send_notification(
            "reminder", "Couldn't add that",
            f"Failed to save to the {rules.reminder_intake_list!r} list — check it exists in Reminders.app.",
        )
        return

    send_notification("reminder", "Added", title)


def _apply_bucket_adjustments(
    bridge: EventKitBridge, bucket_blocks: list[CalendarEvent], adjustments: list
) -> list[CalendarEvent]:
    """Bucket adjustments write directly to the bucket CONTAINER event (in the user's own
    calendar, e.g. Focus/Hobbies) — a different write path from apply_layout, which only
    ever touches agent-authored events in the dedicated agent_calendar. Returns the
    updated bucket_blocks list so the caller doesn't need a fresh (costly) reclassify
    just to see its own just-applied resize."""
    buckets_by_id = {ev.identifier: ev for ev in bucket_blocks}
    for adj in adjustments:
        bucket = buckets_by_id.get(adj.bucket_event_id)
        if bucket is None:
            continue
        bridge.update_event(bucket.identifier, start=adj.new_start, end=adj.new_end)
        logger.info(
            "Resized bucket %r: %s-%s -> %s-%s",
            bucket.title, bucket.start.strftime("%H:%M"), bucket.end.strftime("%H:%M"),
            adj.new_start.strftime("%H:%M"), adj.new_end.strftime("%H:%M"),
        )
        buckets_by_id[adj.bucket_event_id] = replace(bucket, start=adj.new_start, end=adj.new_end)
    return list(buckets_by_id.values())


def _write_incremental_result(bridge: EventKitBridge, rules: Rules, now: datetime, result) -> None:
    """Unlike the day-start pass (which always proposes the full picture for every
    bucket), the incremental prompt is told omitting a bucket means "leave it alone" —
    so only reconcile (and potentially delete stale events in) buckets the model
    actually returned a block for. Passing every agent-authored event here would make
    apply_layout delete untouched blocks in other buckets it never meant to touch.

    Queries the FULL day (via `_today_agent_blocks`, not `now` onward) — caught live: a
    query starting at `now` silently excludes any existing block whose own scheduled end
    already passed relative to real current time, which is true of the TRIGGERING block
    itself on essentially every "running_behind"/"still going" resolution by definition
    (that's the whole premise — its scheduled end already came and the user isn't done).
    apply_layout's key-based matching never found the real existing event to update, so
    it silently CREATED a duplicate instead — a fresh one every single cycle, compounding
    indefinitely on real, ordinary "still going" usage, not just a rare edge case."""
    touched_bucket_ids = {b.bucket_event_id for b in result.blocks}
    existing = [
        e
        for e in _today_agent_blocks(bridge, now, rules)
        if (m := decode_block_meta(e.notes)) and m.get("bucket_event_id") in touched_bucket_ids
    ]
    summary = apply_layout(bridge, result.blocks, existing, rules.agent_calendar, written_at=now)
    logger.info(
        "apply_layout: created=%d updated=%d deleted=%d unchanged=%d",
        len(summary.created), len(summary.updated), len(summary.deleted), len(summary.unchanged),
    )


def run_checkin_answer(
    bridge: EventKitBridge, block_id: str, answer: str,
    state_path: Optional[Path] = None, now: Optional[datetime] = None,
) -> Trigger:
    """answer: 'completed' | 'running_behind' | 'unexpected_plan'. block_id is any
    checkable block's own EventKit identifier — an agent-authored task, a FIXED
    commitment (meeting/class), or an already-specific personal block (Gym/Lunch) —
    whichever was named in the check-in notification.

    `now` optional, defaults to real time — see run_scheduled_tick's docstring."""
    if answer not in ANSWER_TO_LOG_STATUS:
        raise ValueError(f"answer must be one of {list(ANSWER_TO_LOG_STATUS)}, got {answer!r}")

    rules = load_rules(RULES_PATH)
    state = load_state(state_path) if state_path else load_state()
    now = now or datetime.now()

    bucket_blocks, fixed_events, specific_events = _classify_today_events(bridge, now, rules)  # one classify_events call
    agent_blocks = _today_agent_blocks(bridge, now, rules)
    all_checkable = agent_blocks + fixed_events + specific_events
    triggering_task = next((e for e in all_checkable if e.identifier == block_id), None)

    if triggering_task is None:
        # Already resolved or no longer exists — nothing to do, just recompute the trigger.
        checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)
        trigger = compute_next_trigger(
            now, state, rules,
            _compute_next_block_end(checkable, bucket_blocks, now),
            _compute_next_block_start(checkable, now),
        )
        save_state(state, state_path) if state_path else save_state(state)
        return trigger

    meta = decode_block_meta(triggering_task.notes)

    if meta is not None:
        other_agent_blocks = [b for b in agent_blocks if b.identifier != triggering_task.identifier]
        bucket_blocks = _resolve_agent_task(
            bridge, rules, state, now, triggering_task, meta, answer, bucket_blocks, fixed_events, specific_events,
            other_agent_blocks=other_agent_blocks,
        )
    else:
        # A FIXED commitment or an already-specific personal block (Gym, Lunch, a
        # meeting) — never authored or resized by us. "Running behind"/"unexpected" is
        # treated as an external displacement signal for whatever bucket comes next,
        # reusing the same cascade machinery via an "unexpected_plan" resolution applied
        # to THAT bucket instead of trying to touch the external block itself.
        bucket_blocks = _resolve_external_block(
            bridge, rules, state, now, triggering_task, answer, bucket_blocks, fixed_events, specific_events,
            other_agent_blocks=agent_blocks,
        )

    state.last_run_at = now
    agent_blocks = _today_agent_blocks(bridge, now, rules)  # fresh read after any writes above
    checkable = _checkable_blocks(agent_blocks, fixed_events, specific_events, state)
    trigger = compute_next_trigger(
        now, state, rules,
        _compute_next_block_end(checkable, bucket_blocks, now),
        _compute_next_block_start(checkable, now),
    )
    save_state(state, state_path) if state_path else save_state(state)
    return trigger


def _credit_reminder_completion(bridge: EventKitBridge, reminder_id: str, triggering_task: CalendarEvent) -> None:
    """Marks a reminder done in Reminders.app once its work is actually finished —
    immediately for an ordinary single-day task (no tracked progress: `bridge.complete_reminder`
    was previously defined but never called anywhere, a pre-existing gap fixed here for
    both cases), or once a multi-day task's cumulative progress reaches its total
    estimate (see task_progress.py). Best-effort only — the calendar side of the
    check-in already succeeded by the time this runs; never let a failure here surface
    as a failed check-in."""
    try:
        progress = load_task_progress()
        tp = progress.get(reminder_id)
        if tp is None:
            bridge.complete_reminder(reminder_id)
            return
        session_minutes = int((triggering_task.end - triggering_task.start).total_seconds() / 60)
        tp.completed_minutes += session_minutes
        if is_complete(tp):
            bridge.complete_reminder(reminder_id)
            del progress[reminder_id]
            logger.info(
                "Task %s fully credited (%d/%d min) — marked complete in Reminders.app",
                reminder_id, tp.completed_minutes, tp.total_minutes,
            )
        else:
            progress[reminder_id] = tp
            logger.info(
                "Task %s credited %d min this session (%d/%d total so far)",
                reminder_id, session_minutes, tp.completed_minutes, tp.total_minutes,
            )
        save_task_progress(progress)
    except Exception:
        logger.exception("Failed to credit/complete reminder %s", reminder_id)


def _resolve_agent_task(
    bridge: EventKitBridge,
    rules: Rules,
    state: AgentState,
    now: datetime,
    triggering_task: CalendarEvent,
    meta: dict,
    answer: str,
    bucket_blocks: list[CalendarEvent],
    fixed_events: list[CalendarEvent],
    specific_events: Optional[list[CalendarEvent]] = None,
    other_agent_blocks: Optional[list[CalendarEvent]] = None,
) -> list[CalendarEvent]:
    """Case 1: an agent-authored task block inside a bucket. Returns the (possibly
    resized) bucket_blocks list."""
    bucket_event_id = meta.get("bucket_event_id", triggering_task.identifier)
    source = meta.get("source", "reminder")
    source_id = meta.get("source_id")

    # Captured BEFORE replan_incremental (and the apply_layout call inside
    # _write_incremental_result) runs, and used below for completion_log.log_block
    # instead of reading triggering_task.start/.end directly afterward — see issue #27.
    # Some bridge implementations (e.g. FakeEventKitBridge.update_event) mutate the same
    # stored CalendarEvent object in place rather than returning a new one, so if the
    # model's replan proposal gets applied via apply_layout and reuses this exact
    # calendar event id, reading triggering_task.start/.end AFTER the call could silently
    # reflect the NEW proposed time instead of the time this block was actually resolved
    # at — the one thing completion_log exists to guarantee. This is a defense-in-depth
    # backstop, independent of bridge implementation, real or fake.
    triggering_start = triggering_task.start
    triggering_end = triggering_task.end

    if answer == "unexpected_plan":
        # Deterministic, no model call — see ISSUES.md #28's sibling finding: asking the
        # model to intelligently re-fill this task's own reopened slot with a freshly
        # picked/sized reminder reliably produced nothing (0 blocks proposed across 6
        # real calls total, two different test beds, one with a comfortably-fitting
        # candidate reminder and 40 genuinely spare minutes of room). The one
        # unambiguous, judgment-free thing actually needed here doesn't require a model
        # at all: this block no longer represents what's really happening, so the stale
        # placeholder should stop claiming that time. Routed through apply_layout (not a
        # raw bridge.delete_event) specifically so block_snapshots.remove_snapshot fires
        # too, same as every other system-initiated deletion in this codebase — otherwise
        # the now-deleted block's stale snapshot would misread as a manual deletion at
        # the next weekly review. Deliberately does NOT try to pick/size a replacement
        # reminder for the freed time — that's a genuine judgment call (closer to
        # day_layout.plan_day's own job), left for a future pass rather than guessed at
        # here; the freed reminder simply stays open and gets reconsidered naturally.
        apply_layout(bridge, [], [triggering_task], rules.agent_calendar, written_at=now)
        completion_log.log_block(
            triggering_start, triggering_end, triggering_task.title, triggering_task.calendar_title,
            ANSWER_TO_LOG_STATUS[answer], "goal-fill" if source == "goal" else "reminder",
        )
        set_followup(state, now, "unexpected", rules)
        return bucket_blocks

    reminders = _open_reminders(bridge, rules)
    if answer == "completed" and source == "reminder" and source_id:
        # The reminder is being closed out THIS cycle (credited/completed via
        # _credit_reminder_completion a few lines below) — it must not still be a legal
        # candidate for the model to (re)schedule in the very same replan_incremental
        # call. _open_reminders has no way to know it's about to be resolved on its own,
        # since bridge.complete_reminder hasn't actually been called yet at this point in
        # the function. Root-cause fix for issue #27 — narrowly scoped to just this one
        # call's input list.
        reminders = [r for r in reminders if r.identifier != source_id]
    goals = _active_goals()
    goal_candidate = _goal_candidate(rules, goals)
    active_goal_titles = [g.title for g in goals if g.status == "active"]

    result = replan_incremental(
        now, answer, bucket_event_id, bucket_blocks, reminders, rules, goal_candidate,
        fixed_events=fixed_events, triggering_source=source, triggering_source_id=source_id,
        active_goal_titles=active_goal_titles, bucket_context=_full_context(),
        specific_events=specific_events, triggering_block_start=triggering_task.start,
        triggering_block_end=triggering_task.end, other_agent_blocks=other_agent_blocks,
    )
    logger.info(
        "replan_incremental(%s, %s): %d proposed block(s), %d bucket adjustment(s), %d unscheduled reminder(s)",
        answer, bucket_event_id, len(result.blocks), len(result.bucket_adjustments), len(result.unscheduled_reminder_ids),
    )

    bucket_blocks = _apply_bucket_adjustments(bridge, bucket_blocks, result.bucket_adjustments)
    _write_incremental_result(bridge, rules, now, result)

    completion_log.log_block(
        triggering_start, triggering_end, triggering_task.title, triggering_task.calendar_title,
        ANSWER_TO_LOG_STATUS[answer], "goal-fill" if source == "goal" else "reminder",
    )

    if answer == "completed":
        state.pending_followup_at = None
        state.pending_followup_reason = None
        state.resolved_block_ids.add(triggering_task.identifier)
        if source == "reminder" and source_id:
            _credit_reminder_completion(bridge, source_id, triggering_task)
        elif source == "goal" and goal_candidate is not None:
            # Mirrors _credit_reminder_completion's job for the goal-fill case — without
            # this, a goal's last_touched never advances, so pick_goal's staleness weight
            # (goals.py) keeps ranking it by however stale it happened to start, forever,
            # regardless of how many sessions actually get completed on it. Found via
            # graphify's call-graph analysis: record_completion() was defined but had zero
            # call sites anywhere in the codebase — the same "defined but never wired in"
            # gap bridge.complete_reminder() once had (see timeblock-agent-spec.md's
            # decisions log), just never caught live since it only shows up as
            # goal-rotation unfairness over real, multi-day usage, not in one sitting.
            record_completion(goal_candidate, triggering_task.title, when=now)
    else:
        reason = "continuing" if answer == "running_behind" else "unexpected"
        set_followup(state, now, reason, rules)

    return bucket_blocks


def _find_deterministic_cascade_target(
    now: datetime,
    upcoming_buckets: list[CalendarEvent],
    agent_blocks: list[CalendarEvent],
    min_block_minutes: int,
) -> Optional[BucketAdjustment]:
    """Code-only equivalent of the priority-cascade's own SHRINK-FROM-FRONT mechanic
    (rules.md §5's "eat into a lower-priority bucket" shape), applied unconditionally for
    an external block's overrun. Unlike the agent-task overrun case, there's no "other
    side" bucket to weigh priority against — the disruption came from outside the
    schedule entirely — so there's no judgment call to make, only arithmetic: reclaim
    whatever real time has already elapsed (`now` vs. the bucket's own start) from the
    first upcoming bucket that can actually absorb it.

    Replaced a model call here (see incremental_replan.py's own git history/BUILD_LOG —
    the "unexpected_plan" resolution consistently proposed nothing across many real
    Haiku calls, prompt rewrites, and retries, even with a genuinely available reminder
    and ample room). This mechanism doesn't need judgment, only a deterministic rule, so
    it no longer asks for one.

    Tries each upcoming bucket in chronological order (`upcoming_buckets` must already be
    sorted by start); skips one that's already fully consumed by elapsed time, would be
    shrunk below `min_block_minutes`, or already has agent-authored content sitting in it
    (shrinking into already-placed content is a different, higher-risk case this function
    deliberately leaves alone rather than guessing at — that content keeps its own
    check-in later regardless). Returns None if no candidate qualifies — the caller still
    logs/resolves the triggering block itself either way; this only ever concerns the
    NEXT bucket's own boundary."""
    for bucket in upcoming_buckets:
        has_content = any(
            (meta := decode_block_meta(b.notes)) and meta.get("bucket_event_id") == bucket.identifier
            for b in agent_blocks
        )
        if has_content:
            continue
        new_start = max(bucket.start, now)
        if new_start < bucket.end and (bucket.end - new_start) >= timedelta(minutes=min_block_minutes):
            return BucketAdjustment(bucket_event_id=bucket.identifier, new_start=new_start, new_end=bucket.end)
    return None


def _resolve_external_block(
    bridge: EventKitBridge,
    rules: Rules,
    state: AgentState,
    now: datetime,
    triggering_task: CalendarEvent,
    answer: str,
    bucket_blocks: list[CalendarEvent],
    fixed_events: list[CalendarEvent],
    specific_events: Optional[list[CalendarEvent]] = None,
    other_agent_blocks: Optional[list[CalendarEvent]] = None,
) -> list[CalendarEvent]:
    """Case 2/3: a FIXED commitment or FLEXIBLE_SPECIFIC block we never authored or
    resize ourselves. Logs + resolves it; if it ran over, deterministically shrinks the
    next chronological bucket's own front to reclaim the elapsed time (see
    _find_deterministic_cascade_target) instead of touching this block. No model call —
    this is pure arithmetic, not a judgment call. Returns the (possibly resized)
    bucket_blocks list."""
    completion_log.log_block(
        triggering_task.start, triggering_task.end, triggering_task.title, triggering_task.calendar_title,
        ANSWER_TO_LOG_STATUS[answer], "external",
    )
    state.resolved_block_ids.add(triggering_task.identifier)

    if answer in ("running_behind", "unexpected_plan"):
        upcoming_buckets = sorted((b for b in bucket_blocks if b.start >= triggering_task.end), key=lambda b: b.start)
        adjustment = _find_deterministic_cascade_target(now, upcoming_buckets, other_agent_blocks or [], rules.min_block_minutes)
        if adjustment is not None:
            buckets_by_id = {b.identifier: b for b in bucket_blocks}
            hard_cutoff = datetime.combine(now.date(), rules.evening_checkin_floor)
            if validate_bucket_adjustment(
                adjustment, buckets_by_id, hard_cutoff, fixed_events, specific_events,
                min_block_minutes=rules.min_block_minutes,
            ):
                bucket = buckets_by_id[adjustment.bucket_event_id]
                logger.info(
                    "Deterministic external cascade: %r (%s) -> shrank %r %s-%s to %s-%s",
                    triggering_task.title, answer, bucket.title,
                    bucket.start.strftime("%H:%M"), bucket.end.strftime("%H:%M"),
                    adjustment.new_start.strftime("%H:%M"), adjustment.new_end.strftime("%H:%M"),
                )
                bucket_blocks = _apply_bucket_adjustments(bridge, bucket_blocks, [adjustment])
            else:
                logger.warning(
                    "Deterministic external cascade: computed adjustment for %r failed "
                    "validate_bucket_adjustment — leaving buckets untouched", adjustment.bucket_event_id,
                )

        reason = "continuing" if answer == "running_behind" else "unexpected"
        set_followup(state, now, reason, rules)
    else:
        state.pending_followup_at = None
        state.pending_followup_reason = None

    return bucket_blocks
