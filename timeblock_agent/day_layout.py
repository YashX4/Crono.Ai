"""Day-layout planning: fill today's FLEXIBLE_BUCKET blocks with specific task blocks
pulled from open Reminders (or a goal, when nothing else fits), in one Claude call
(see spec: "Day-Start Full Plan" / "Claude prompt: classify Reminders -> open FLEXIBLE
buckets"). Which blocks count as FLEXIBLE_BUCKET is decided upstream, per-cycle, by
classify.classify_events — this module just fills the ones it's handed.

Protected-buffer matching (blocks the user never wants task-filled, e.g. wind-down time)
is deterministic keyword matching against rules.yaml, since that's explicit user intent
rather than something to infer from content.

Goal-fill candidate selection is likewise pre-computed by goals.pick_goal() before this
call; the model is only ever shown the one goal that was already picked (see
Cost & Budget guardrail: never send full goal bodies, only frontmatter + next_action).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from timeblock_agent.claude_client import MODEL_HAIKU, get_client
from timeblock_agent.config import ProtectedBuffer, Rules
from timeblock_agent.eventkit_bridge import CalendarEvent, Reminder
from timeblock_agent.goals import Goal
from timeblock_agent.task_progress import PacingInfo

logger = logging.getLogger("timeblock_agent.day_layout")

_GOAL_TIME_META_RE = re.compile(r"<!-- goal-time-meta: (\{.*?\}) -->")


def encode_goal_time_meta(sourced_from_bucket_event_id: str) -> str:
    """Marker embedded in a synthetic "Goal Time" bucket's own `notes` field, recording
    which real bucket it was carved from — read later so an overrun on the donor bucket
    knows to eat this one first (see incremental_replan.py), without re-deriving it."""
    return f"<!-- goal-time-meta: {json.dumps({'sourced_from_bucket_event_id': sourced_from_bucket_event_id})} -->"


def decode_goal_time_meta(notes: Optional[str]) -> Optional[str]:
    """Returns the donor bucket's event_id if `notes` carries a Goal Time provenance
    marker, else None. Distinct from diff_write.decode_block_meta, which is for
    agent-authored task blocks, not bucket containers."""
    if not notes:
        return None
    m = _GOAL_TIME_META_RE.search(notes)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("sourced_from_bucket_event_id")
    except json.JSONDecodeError:
        return None

_SYSTEM_TEMPLATE = """\
You are the day-layout planner for a time-blocking agent. You fill FLEXIBLE_BUCKET blocks \
on the user's calendar with specific task blocks pulled from their open Reminders, for the \
remainder of today only — never before `now`. Each bucket's own title is your only signal \
for what kind of task belongs in it (e.g. "Work" wants a work-shaped reminder, \
"HobbyMaxxing" wants a hobby-shaped one) — use judgment, there's no fixed category list.

Rules, in priority order:
1. For every block with `is_protected_buffer: false` (the normal case), always try to fill \
it with the best-fitting open reminder(s) first. Attempt every such block — don't leave one \
empty just because you already used a reminder elsewhere, if another reasonable candidate \
remains. Size each task block to how long the task would REALISTICALLY take — don't \
stretch a quick task to fill the whole bucket, and don't compress a genuinely long one \
just because the bucket looks small. Leftover room after realistic sizing is real \
leftover room, not something to eliminate. Reminders and goal-fill are never both used \
for the same block. Place blocks in chronological order, filling the EARLIEST available \
room in the bucket first — never leave an earlier gap unused only to then squeeze a later \
task into less room than it realistically needs elsewhere in the same bucket's remaining \
time; a task's size should come from its own realistic estimate, never from however much \
room happens to be left wherever you chose to place it.
2. If a reminder realistically needs more time than fits in one instance of its bucket \
today, and another instance of a similarly-shaped bucket exists later today with room, \
split it across both — the SAME `source_id`, as two separate blocks — rather than \
truncating it or leaving the rest undone. If it still doesn't fit anywhere today at all, \
that's fine: put it in `unscheduled_reminder_ids` — UNLESS it has `today_target_minutes` \
given below (a multi-day task already being paced against its deadline), in which case \
schedule roughly that much today specifically, even if the task's full realistic size is \
much bigger; `at_risk`/`overdue` on that reminder means push its target higher today, \
ahead of same-priority reminders that aren't at risk. A reminder with `today_target_minutes` \
is never allowed to end up with ZERO minutes scheduled while ANY bucket today (not just \
ones already holding its own kind of task) still has genuinely unused leftover room in \
it, even a partial chunk smaller than the full target — check every bucket's actual \
remaining slack before concluding there's truly nowhere for it, since leaving it fully \
unscheduled defeats the entire point of flagging it at-risk in the first place.
3. If a bucket's realistically-sized reminder workload doesn't fit its own window at all \
(nowhere else today to split into, nothing reasonable to bump instead), propose a \
`bucket_adjustments` entry extending that bucket's own end to fit — never past \
`hard_cutoff`, and never overlapping any `fixed_events` given below. This is ONLY for \
genuine reminder workload that doesn't fit — never extend a bucket's window just to make \
room for goal work; goal time is sourced separately, after this pass, from whatever is \
actually left over, and is not your concern here.
4. `allow_goal_fill` is only ever true for a block with `is_protected_buffer: true` — a \
goal-fill block may ONLY be placed where `allow_goal_fill` is true, never in an ordinary \
bucket, even if no reminder happens to fit it (leave it unfilled instead). Where allowed, \
place at most one goal-fill block total across the whole day, using the goal's \
`next_action` as the title, sized to reasonably fit the block. Never invent a goal-fill \
block if no `goal_candidate` is given.
5. Every task block must fit entirely inside the bucket block it belongs to (same or \
narrower start/end, or the new end from a `bucket_adjustments` entry you're also \
proposing this cycle), and be at least {min_block_minutes} minutes long.
6. Leave roughly {buffer_block_minutes} minutes between back-to-back task blocks inside \
the same bucket when there's slack to do so; don't force it if the bucket is tight.
7. Among candidate reminders for a block, prioritize earlier due dates and higher \
priority (lower number = higher priority in EventKit's scale, 0 = none).
8. Never propose a block outside of a given bucket's own (possibly adjusted) start/end \
window, and never a block starting before `now`.

Separately, tag every bucket in `bucket_roles`: `goal_time_eligible: true` if it reads as \
work/obligation-shaped and nothing says otherwise, `false` if it's leisure-shaped or the \
user's own description below says it should never give up time for goal work (e.g. a \
dedicated exam-revision block). This is informational only — you are not sourcing or \
reserving any goal time yourself here, just reporting which buckets could be raided for \
it afterward:

{bucket_context}

Also report `task_estimates` for EVERY reminder you were given, whether or not you \
managed to schedule it this cycle, and whether or not it has a deadline. For each: your \
own realistic estimate of its FULL total duration (not just today's chunk if you split \
or paced it — the whole task) and its deadline if it has one — either its own `due_date` \
field, or a date you can recognize in its title/notes text (e.g. "Essay - due Friday"), \
else `null`/`false`. This is purely informational from your side — whether a task \
actually gets tracked across days, or whether its scheduled time is checked against this \
estimate, is decided in code afterward, not by you.

Always call the propose_day_layout tool exactly once with your full result.
"""

_DAY_LAYOUT_TOOL = {
    "name": "propose_day_layout",
    "description": "Propose task blocks filling today's FLEXIBLE_BUCKET blocks for the remaining day.",
    "input_schema": {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bucket_event_id": {"type": "string"},
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "ISO 8601 datetime"},
                        "end": {"type": "string", "description": "ISO 8601 datetime"},
                        "source": {"type": "string", "enum": ["reminder", "goal"]},
                        "source_id": {
                            "type": "string",
                            "description": "reminder_id for source=reminder, or 'goal' for source=goal",
                        },
                    },
                    "required": ["bucket_event_id", "title", "start", "end", "source", "source_id"],
                },
            },
            "unscheduled_reminder_ids": {"type": "array", "items": {"type": "string"}},
            "bucket_adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bucket_event_id": {"type": "string"},
                        "new_start": {"type": "string", "description": "ISO 8601 datetime"},
                        "new_end": {"type": "string", "description": "ISO 8601 datetime"},
                    },
                    "required": ["bucket_event_id", "new_start", "new_end"],
                },
            },
            "bucket_roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "goal_time_eligible": {"type": "boolean"},
                    },
                    "required": ["event_id", "goal_time_eligible"],
                },
            },
            "task_estimates": {
                "type": "array",
                "description": "One entry per reminder you were given, whether or not scheduled, whether or not it has a deadline.",
                "items": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {"type": "string"},
                        "realistic_total_minutes": {
                            "type": "integer",
                            "description": "Your estimate of the task's FULL duration, not just today's chunk.",
                        },
                        "due_date": {
                            "type": ["string", "null"],
                            "description": "ISO 8601 date (native due_date or recognized from title/notes text), or null if none.",
                        },
                        "is_overdue": {"type": "boolean"},
                    },
                    "required": ["reminder_id", "realistic_total_minutes", "due_date", "is_overdue"],
                },
            },
        },
        "required": ["blocks", "unscheduled_reminder_ids", "bucket_adjustments", "bucket_roles", "task_estimates"],
    },
}


@dataclass
class ProposedBlock:
    bucket_event_id: str
    title: str
    start: datetime
    end: datetime
    source: str  # "reminder" | "goal"
    source_id: str


@dataclass
class BucketAdjustment:
    """A proposed resize/move of a bucket CONTAINER itself (e.g. extending "Work" past
    its original end when a task genuinely overran with nowhere left inside the
    original window) — distinct from ProposedBlock, which only fills task content
    within a bucket's existing window. Buckets are classified FLEXIBLE specifically
    because they're fair game to move/resize, not just the task blocks inside them."""

    bucket_event_id: str
    new_start: datetime
    new_end: datetime


def overlaps_fixed_event(start: datetime, end: datetime, fixed_events: list[CalendarEvent]) -> bool:
    """FIXED events (real meetings, classes, commitments) are never visible to the model
    as something it's filling — this is the code-level backstop making sure a bucket
    resize or cascade can never land on top of one, independent of prompt wording."""
    return any(start < fe.end and end > fe.start for fe in fixed_events)


def validate_bucket_adjustment(
    adjustment: BucketAdjustment,
    buckets_by_id: dict[str, CalendarEvent],
    cutoff: datetime,
    fixed_events: Optional[list[CalendarEvent]] = None,
    other_events: Optional[list[CalendarEvent]] = None,
    min_block_minutes: Optional[int] = None,
) -> bool:
    """cutoff is the hard end-of-day boundary (e.g. today's evening_checkin_floor) that
    cascading may never push past — overflow past this point should leave the
    lowest-priority content unscheduled (bumped to tomorrow) instead.

    Exactly three shapes are legal — anything else (moving both ends by DIFFERENT
    amounts, moving neither, moving the wrong direction) is a relocation/shrink-in-place
    and gets rejected:
    - EXTEND (own end grows, start untouched): used when a bucket itself is overrunning
      and pulling in its own slack. `new_start` must be EXACTLY the bucket's current
      start; `new_end` must be strictly later than its current end.
    - SHRINK-FROM-FRONT (start pushed later, end untouched): used by incremental_replan's
      priority cascade (rule 4's second branch) when a LOWER-priority bucket gives up its
      own front time to whatever overran before it. `new_end` must be EXACTLY the
      bucket's current end; `new_start` must be strictly later than its current start,
      leaving at least `min_block_minutes` of room if given.
    - PUSH (both ends shift later by the same amount, duration preserved): used by
      incremental_replan's priority cascade (rule 4's first branch) when the NEXT bucket
      is judged EQUAL-OR-HIGHER priority than the one overrunning — it gets delayed
      instead of losing time. `new_start` must be strictly later than the bucket's
      current start, and `new_end - new_start` must exactly equal the bucket's own
      current duration. Missing this shape entirely was a real bug: rule 4's first
      branch could never actually succeed, since neither EXTEND nor SHRINK-FROM-FRONT
      matches "both ends move by the same delta" — every push-later proposal failed
      validation regardless of correctness (caught live).
    Requiring an exact match / exact duration (not just `>=`/`<=` the old bound) is
    deliberately strict: a looser check previously let a proposal move BOTH ends at once
    and shrink/relocate the whole window into something unrelated to the original bucket
    — caught live, e.g. a 60-minute bucket "resized" into an unrelated 15-minute window
    starting after its own original end.
    """
    bucket = buckets_by_id.get(adjustment.bucket_event_id)
    if bucket is None:
        return False
    if adjustment.new_end <= adjustment.new_start:
        return False

    if adjustment.new_start == bucket.start and adjustment.new_end > bucket.end:
        # EXTEND shape.
        if adjustment.new_end > cutoff:
            return False
        # Only the newly-ADDED portion (past the bucket's original end) needs to avoid a
        # fixed event — the original span may already legitimately contain one nested
        # inside it (e.g. a meeting mid-bucket, by design in the test sandbox), which is
        # fine and must not permanently block extending the tail. Checking the whole
        # proposed span here was a real bug: it made any bucket with a nested fixed event
        # unextendable forever, since the extension always "overlaps" the event that was
        # already there.
        if fixed_events and overlaps_fixed_event(bucket.end, adjustment.new_end, fixed_events):
            return False
        # Same newly-added-portion-only check, but against everything else already on the
        # calendar today (other FLEXIBLE_BUCKET containers, FLEXIBLE_SPECIFIC blocks like
        # Gym/Lunch, other already-placed agent task blocks) — not just real FIXED
        # commitments. Missing entirely was a real bug: an extension could silently run
        # straight through an adjacent already-scheduled block (caught live: a bucket's
        # extension overlapped both a neighboring Gym block and a protected buffer that
        # happened to sit right after it).
        if other_events and overlaps_fixed_event(bucket.end, adjustment.new_end, other_events):
            return False
        return True

    if adjustment.new_end == bucket.end and adjustment.new_start > bucket.start:
        # SHRINK-FROM-FRONT shape. No fixed/other-event check against the newly-EXCLUDED
        # front portion is needed here — giving that time away can't create an overlap by
        # itself; if the encroaching task ends up overlapping something that already lived
        # in this bucket's front (e.g. a task already placed at the bucket's original
        # start), that's caught separately by checking the encroaching block itself
        # against other_agent_blocks, not by this function.
        if min_block_minutes is not None and (adjustment.new_end - adjustment.new_start).total_seconds() < min_block_minutes * 60:
            return False
        return True

    if adjustment.new_start > bucket.start and (adjustment.new_end - adjustment.new_start) == (bucket.end - bucket.start):
        # PUSH shape. The whole window has moved to new territory (except where it
        # overlaps its OWN old position, which is safe by construction — it's being
        # moved, not duplicated) — check the full new span, not just an added portion.
        if adjustment.new_end > cutoff:
            return False
        if fixed_events and overlaps_fixed_event(adjustment.new_start, adjustment.new_end, fixed_events):
            return False
        if other_events and overlaps_fixed_event(adjustment.new_start, adjustment.new_end, other_events):
            return False
        return True

    return False


def match_protected_buffer(event: CalendarEvent, rules: Rules) -> Optional[ProtectedBuffer]:
    haystack = f"{event.title} {event.calendar_title}".lower()
    for buf in rules.protected_buffers:
        if any(m.lower() in haystack for m in buf.match):
            return buf
    return None


def _build_system_prompt(rules: Rules, bucket_context: str) -> str:
    return _SYSTEM_TEMPLATE.format(
        min_block_minutes=rules.min_block_minutes,
        buffer_block_minutes=rules.buffer_block_minutes,
        bucket_context=bucket_context or "(none configured yet — use judgment from titles alone)",
    )


def _build_user_payload(
    now: datetime,
    bucket_blocks: list[CalendarEvent],
    reminders: list[Reminder],
    rules: Rules,
    goal_candidate: Optional[Goal],
    fixed_events: list[CalendarEvent],
    hard_cutoff: datetime,
    task_pacing: dict[str, PacingInfo],
) -> dict:
    flexible_payload = []
    for ev in bucket_blocks:
        buffer_ = match_protected_buffer(ev, rules)
        flexible_payload.append(
            {
                "event_id": ev.identifier,
                "title": ev.title,
                "start": ev.start.isoformat(),
                "end": ev.end.isoformat(),
                "is_protected_buffer": buffer_ is not None,
                "allow_goal_fill": buffer_.allow_goal_fill if buffer_ else False,
            }
        )

    reminders_payload = []
    for r in reminders:
        entry = {
            "reminder_id": r.identifier,
            "title": r.title,
            "due_date": r.due_date.date().isoformat() if r.due_date else None,
            "priority": r.priority,
        }
        pacing = task_pacing.get(r.identifier)
        if pacing is not None:
            entry["today_target_minutes"] = pacing.today_target_minutes
            entry["remaining_minutes"] = pacing.remaining_minutes
            entry["days_remaining"] = pacing.days_remaining
            entry["at_risk"] = pacing.at_risk
            entry["overdue"] = pacing.overdue
        reminders_payload.append(entry)

    goal_payload = None
    if goal_candidate is not None:
        goal_payload = {"title": goal_candidate.title, "next_action": goal_candidate.next_action}

    fixed_payload = [
        {"title": ev.title, "start": ev.start.isoformat(), "end": ev.end.isoformat()} for ev in fixed_events
    ]

    return {
        "now": now.isoformat(),
        "flexible_blocks": flexible_payload,
        "open_reminders": reminders_payload,
        "goal_candidate": goal_payload,
        "fixed_events": fixed_payload,
        "hard_cutoff": hard_cutoff.isoformat(),
    }


def resolve_authoritative_title(
    block: ProposedBlock, reminders_by_id: dict[str, Reminder], goal_candidate: Optional[Goal]
) -> Optional[str]:
    """Never trust the model's freeform `title` for what actually gets written to the
    calendar — derive it from the real reminder/goal the block claims to come from
    instead. Returns None if the claimed source doesn't actually resolve (e.g. a
    source_id that isn't a real open reminder), which the caller should treat as
    grounds to reject the block outright rather than trust an unverifiable title."""
    if block.source == "reminder":
        reminder = reminders_by_id.get(block.source_id)
        return reminder.title if reminder else None
    if block.source == "goal":
        return goal_candidate.next_action if goal_candidate else None
    return None


def validate_block(
    block: ProposedBlock,
    buckets_by_id: dict[str, CalendarEvent],
    rules: Rules,
    now: datetime,
    goal_candidate_given: bool = True,
    fixed_events: Optional[list[CalendarEvent]] = None,
) -> bool:
    """Enforced in code, not just prompt text — a smaller model won't always honor a
    "never do X unless Y" instruction, so goal-fill eligibility gets rechecked here from
    the same rules.yaml protected_buffers config the prompt was built from, independent
    of what the model claims. Same reasoning applies to `block.start < now`: both
    plan_day's and incremental_replan's own prompts already say "never before now," but
    caught live (TESTING_LOG.md Session 5, Scenario 11) — a day-start pass that ran late
    (17+ real minutes after its own bucket-seed times, simulating a delayed wake) placed
    a task block starting well before the `now` it was explicitly given, with nothing in
    code to catch it. This function is shared by both plan_day and replan_incremental
    (imported directly, not reimplemented), so fixing it here protects both call sites
    at once."""
    bucket_event = buckets_by_id.get(block.bucket_event_id)
    if bucket_event is None:
        return False
    if block.start < bucket_event.start or block.end > bucket_event.end:
        return False
    if block.start < now:
        return False
    if block.end <= block.start:
        return False
    if (block.end - block.start).total_seconds() < rules.min_block_minutes * 60:
        return False
    buffer_ = match_protected_buffer(bucket_event, rules)
    if block.source == "goal":
        if not goal_candidate_given:
            return False
        allow_goal_fill = buffer_.allow_goal_fill if buffer_ else False
        if not allow_goal_fill:
            return False
    elif block.source == "reminder" and buffer_ is not None:
        # The reverse direction of the check above: a protected buffer (or a synthetic
        # "Goal Time" bucket, which is always one) never gets a regular reminder, no
        # matter what the model proposes — previously only prompt wording enforced this.
        return False
    if fixed_events and overlaps_fixed_event(block.start, block.end, fixed_events):
        return False
    return True


_PRIORITY_TOOL = {
    "name": "rank_bucket_priority",
    "description": "Rank today's FLEXIBLE_BUCKET blocks from lowest to highest priority.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ranked_event_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Every given bucket_event_id, ordered LOWEST priority first, HIGHEST priority last.",
            }
        },
        "required": ["ranked_event_ids"],
    },
}

_PRIORITY_SYSTEM_TEMPLATE = """\
You judge the relative priority of today's open-ended bucket blocks, lowest to highest — \
this is used to decide which bucket gives up time today for something else (e.g. \
dedicated goal work the user explicitly asked for). Judge from each bucket's title and \
the user's own descriptions below, if any — trust these over guessing from title-shape \
when they say something specific:

{bucket_context}

Absent specific guidance, work-shaped/obligation-like buckets generally outrank \
leisure-shaped ones, but use judgment, not a fixed rule.

Always call rank_bucket_priority exactly once, including every given bucket_event_id \
exactly once, ordered from lowest priority first to highest priority last.
"""


def rank_buckets_by_priority(bucket_blocks: list[CalendarEvent], bucket_context: str = "") -> list[str]:
    """Returns bucket_event_ids ordered lowest-priority-first. Raises on API failure —
    caller should treat that as "no safe pick" and skip whatever it was about to do
    (e.g. carving out goal time) rather than guess which bucket to sacrifice."""
    if len(bucket_blocks) <= 1:
        return [e.identifier for e in bucket_blocks]

    client = get_client()
    system_prompt = _PRIORITY_SYSTEM_TEMPLATE.format(
        bucket_context=bucket_context or "(none configured yet — use judgment from titles alone)"
    )
    payload = [{"event_id": e.identifier, "title": e.title} for e in bucket_blocks]

    response = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=1024,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_PRIORITY_TOOL],
        tool_choice={"type": "tool", "name": "rank_bucket_priority"},
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    valid_ids = {e.identifier for e in bucket_blocks}
    ranked = [eid for eid in tool_use.input["ranked_event_ids"] if eid in valid_ids]
    # Safety net: any bucket the model omitted goes at the end (treated as higher
    # priority / not the one to sacrifice) rather than risk picking an unjudged bucket.
    for eid in valid_ids:
        if eid not in ranked:
            ranked.append(eid)
    return ranked


@dataclass
class TaskEstimate:
    reminder_id: str
    realistic_total_minutes: int
    due_date: Optional[str]  # ISO date string, or None
    is_overdue: bool


@dataclass
class DayLayoutResult:
    blocks: list[ProposedBlock]
    unscheduled_reminder_ids: list[str]
    bucket_adjustments: list[BucketAdjustment]
    bucket_roles: dict[str, bool]  # bucket event_id -> goal_time_eligible
    task_estimates: list[TaskEstimate]


def plan_day(
    now: datetime,
    bucket_blocks: list[CalendarEvent],
    reminders: list[Reminder],
    rules: Rules,
    goal_candidate: Optional[Goal] = None,
    fixed_events: Optional[list[CalendarEvent]] = None,
    bucket_context: str = "",
    task_pacing: Optional[dict[str, PacingInfo]] = None,
    specific_events: Optional[list[CalendarEvent]] = None,
) -> DayLayoutResult:
    """`bucket_blocks` should already be filtered to events classify.classify_events
    tagged FLEXIBLE_BUCKET. `fixed_events` are today's real commitments — required for
    the code-level overlap check on any proposed `bucket_adjustments`, the same as
    `incremental_replan`. `specific_events` (FLEXIBLE_SPECIFIC — Gym, Lunch, etc.) are
    required for that same check for a different reason: they're not "real commitments"
    in the FIXED sense, but they're still already-scheduled blocks a bucket extension
    must never silently run over (caught live: an extension overlapped both a
    neighboring specific block and another bucket that happened to sit right after it —
    `other_bucket_events` below covers both that and every OTHER bucket container too).
    `task_pacing` (see task_progress.py) gives Python-computed today-targets for
    reminders already being paced against a deadline across multiple days — absent
    entries just get today's normal best-effort treatment. Raises on API failure —
    caller must no-op (leave the calendar untouched) rather than guess."""
    if not bucket_blocks:
        return DayLayoutResult([], [r.identifier for r in reminders], [], {}, [])

    fixed_events = fixed_events or []
    specific_events = specific_events or []
    other_bucket_events = specific_events + bucket_blocks
    task_pacing = task_pacing or {}
    hard_cutoff = datetime.combine(now.date(), rules.evening_checkin_floor)

    client = get_client()
    system_prompt = _build_system_prompt(rules, bucket_context)
    user_payload = _build_user_payload(
        now, bucket_blocks, reminders, rules, goal_candidate, fixed_events, hard_cutoff, task_pacing
    )

    response = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=8192,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_DAY_LAYOUT_TOOL],
        tool_choice={"type": "tool", "name": "propose_day_layout"},
        messages=[{"role": "user", "content": json.dumps(user_payload, indent=2)}],
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    buckets_by_id = {ev.identifier: ev for ev in bucket_blocks}
    reminders_by_id = {r.identifier: r for r in reminders}

    # Bucket adjustments are validated and applied to a local copy of buckets_by_id
    # BEFORE validating blocks against it — a block extending into a newly-widened
    # window is legitimate exactly when the widening itself is legitimate.
    adjustments: list[BucketAdjustment] = []
    for item in tool_use.input.get("bucket_adjustments", []):
        adjustment = BucketAdjustment(
            bucket_event_id=item["bucket_event_id"],
            new_start=datetime.fromisoformat(item["new_start"]),
            new_end=datetime.fromisoformat(item["new_end"]),
        )
        if validate_bucket_adjustment(adjustment, buckets_by_id, hard_cutoff, fixed_events, other_bucket_events):
            adjustments.append(adjustment)
            buckets_by_id[adjustment.bucket_event_id] = replace(
                buckets_by_id[adjustment.bucket_event_id], start=adjustment.new_start, end=adjustment.new_end
            )
        else:
            bucket_event = buckets_by_id.get(adjustment.bucket_event_id)
            logger.warning(
                "plan_day: rejected bucket_adjustment for %r (%s-%s -> %s-%s) — failed validate_bucket_adjustment",
                bucket_event.title if bucket_event else adjustment.bucket_event_id,
                bucket_event.start if bucket_event else "?", bucket_event.end if bucket_event else "?",
                adjustment.new_start, adjustment.new_end,
            )

    blocks: list[ProposedBlock] = []
    for item in tool_use.input["blocks"]:
        block = ProposedBlock(
            bucket_event_id=item["bucket_event_id"],
            title=item["title"],
            start=datetime.fromisoformat(item["start"]),
            end=datetime.fromisoformat(item["end"]),
            source=item["source"],
            source_id=item["source_id"],
        )
        resolved_title = resolve_authoritative_title(block, reminders_by_id, goal_candidate)
        if resolved_title is None:
            logger.warning(
                "plan_day: rejected proposed block %r (%s-%s) — source %r/%r didn't resolve to a real reminder/goal",
                item.get("title"), block.start, block.end, block.source, block.source_id,
            )
            continue  # claimed source doesn't actually resolve — reject rather than trust it
        block.title = resolved_title
        if validate_block(block, buckets_by_id, rules, now, goal_candidate_given=goal_candidate is not None, fixed_events=fixed_events):
            blocks.append(block)
        else:
            bucket_event = buckets_by_id.get(block.bucket_event_id)
            logger.warning(
                "plan_day: rejected proposed block %r (%s-%s, source=%s/%s) — failed validate_block "
                "against bucket %r (%s-%s)",
                block.title, block.start, block.end, block.source, block.source_id,
                bucket_event.title if bucket_event else block.bucket_event_id,
                bucket_event.start if bucket_event else "?", bucket_event.end if bucket_event else "?",
            )

    unscheduled = list(tool_use.input["unscheduled_reminder_ids"])

    # Safety net: a reminder the model tried to place but got rejected by validate_block
    # (e.g. an internally-inconsistent proposal — a block assuming room from a bucket
    # adjustment it never actually requested) would otherwise vanish with no trace: not
    # placed, not reported as unscheduled either, since the model believed it had handled
    # it. Reconcile in code — never trust the model's own bookkeeping to be complete —
    # so nothing a user is waiting on silently disappears for the day.
    placed_reminder_ids = {b.source_id for b in blocks if b.source == "reminder"}
    accounted_for = placed_reminder_ids | set(unscheduled)
    for r in reminders:
        if r.identifier not in accounted_for:
            logger.warning(
                "plan_day: reminder %r not scheduled and not reported as unscheduled by "
                "the model — added to unscheduled_reminder_ids in code",
                r.title,
            )
            unscheduled.append(r.identifier)

    # Safety net: any bucket the model didn't tag defaults to NOT goal-time-eligible —
    # fail closed rather than risk exposing an unjudged (possibly leisure) bucket to
    # donation by omission.
    valid_ids = {e.identifier for e in bucket_blocks}
    bucket_roles = {eid: False for eid in valid_ids}
    for item in tool_use.input.get("bucket_roles", []):
        if item["event_id"] in valid_ids:
            bucket_roles[item["event_id"]] = bool(item["goal_time_eligible"])

    task_estimates = [
        TaskEstimate(
            reminder_id=item["reminder_id"],
            realistic_total_minutes=item["realistic_total_minutes"],
            due_date=item.get("due_date"),
            is_overdue=bool(item.get("is_overdue", False)),
        )
        for item in tool_use.input.get("task_estimates", [])
        if item["reminder_id"] in reminders_by_id
    ]

    # Cross-check: a NON-paced reminder's total scheduled time should roughly match its
    # own claimed realistic size (the prompt already says so — rule 1 — but a smaller
    # model doesn't always honor it in one shot; caught live twice already, a reminder
    # squashed down to whatever room happened to be left rather than its own realistic
    # estimate). A paced multi-day task is exempt — scheduling less than the full size on
    # purpose (today_target_minutes) is correct there, not a squash.
    scheduled_minutes_by_reminder: dict[str, float] = {}
    for b in blocks:
        if b.source == "reminder":
            scheduled_minutes_by_reminder[b.source_id] = (
                scheduled_minutes_by_reminder.get(b.source_id, 0) + (b.end - b.start).total_seconds() / 60
            )

    estimates_by_reminder = {te.reminder_id: te for te in task_estimates}
    blocks_by_reminder: dict[str, list[ProposedBlock]] = {}
    for b in blocks:
        if b.source == "reminder":
            blocks_by_reminder.setdefault(b.source_id, []).append(b)

    squashed_ids: set[str] = set()
    corrected_blocks: dict[str, ProposedBlock] = {}
    for reminder_id, scheduled in scheduled_minutes_by_reminder.items():
        if task_pacing and reminder_id in task_pacing:
            continue  # deliberately partial — paced against a real deadline, not a squash
        est = estimates_by_reminder.get(reminder_id)
        if est is None or est.realistic_total_minutes <= 0:
            continue  # model didn't report an estimate for this one — nothing to check
        if scheduled >= est.realistic_total_minutes * 0.75:
            continue

        # Squashed. Before giving up on the reminder entirely, try a deterministic,
        # code-only correction: same bucket/start the model already proposed, just sized
        # to the reminder's own claimed realistic duration instead of whatever room the
        # model happened to leave — no new model call needed, both facts are already
        # known. Only attempted when the model proposed exactly one block for it (a
        # squash split across multiple blocks is rare and safer to just leave unscheduled
        # than to guess how to re-slice it). Caught live: a reminder squashed to half its
        # real size was rejected and fell to unscheduled even though the exact room it
        # needed was sitting right there, unused, for the rest of the day.
        candidates = blocks_by_reminder.get(reminder_id, [])
        corrected = None
        if len(candidates) == 1:
            original = candidates[0]
            attempt = replace(original, end=original.start + timedelta(minutes=est.realistic_total_minutes))
            # validate_block only checks bucket bounds/fixed events, never sibling
            # reminder blocks in the same bucket (the model is normally trusted not to
            # propose overlapping ones itself) — growing a squashed block's size
            # specifically risks colliding with a neighbor that was placed assuming the
            # squashed (smaller) size, so that has to be checked explicitly here.
            no_sibling_overlap = not any(
                other is not original and other.start < attempt.end and other.end > attempt.start
                for other in blocks
            )
            if no_sibling_overlap and validate_block(
                attempt, buckets_by_id, rules, now, goal_candidate_given=goal_candidate is not None, fixed_events=fixed_events
            ):
                corrected = attempt

        if corrected is not None:
            logger.info(
                "plan_day: reminder %r was squashed to %.0f min — corrected in code to "
                "its own claimed %d min, which fits without conflict",
                reminders_by_id[reminder_id].title, scheduled, est.realistic_total_minutes,
            )
            corrected_blocks[reminder_id] = corrected
        else:
            logger.warning(
                "plan_day: reminder %r scheduled for only %.0f of its own claimed %d "
                "realistic minutes (not a paced multi-day task), and its own size doesn't "
                "fit either — treating as squashed-to-fit, moving to "
                "unscheduled_reminder_ids instead of keeping a wrongly-sized block",
                reminders_by_id[reminder_id].title, scheduled, est.realistic_total_minutes,
            )
            squashed_ids.add(reminder_id)

    if squashed_ids or corrected_blocks:
        drop_ids = squashed_ids | set(corrected_blocks)
        blocks = [b for b in blocks if not (b.source == "reminder" and b.source_id in drop_ids)]
        blocks += list(corrected_blocks.values())
        for rid in squashed_ids:
            if rid not in unscheduled:
                unscheduled.append(rid)

    return DayLayoutResult(blocks, unscheduled, adjustments, bucket_roles, task_estimates)
