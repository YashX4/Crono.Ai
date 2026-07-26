"""Event-driven incremental replan (see spec: "Event-Driven Incremental Replan").

Triggered by a block ending/overrunning, a follow-up delay elapsing, the hourly
fallback, or a check-in answer arriving via the webhook. Adjusts only the block that
triggered this cycle plus whatever must cascade after it — everything else on the
calendar is left untouched. The whole-day pass (day_layout.plan_day) only ever runs once,
at day-start; this is what runs the rest of the day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from timeblock_agent.claude_client import MODEL_HAIKU, get_client
from timeblock_agent.config import Rules
from timeblock_agent.day_layout import (
    BucketAdjustment,
    ProposedBlock,
    decode_goal_time_meta,
    match_protected_buffer,
    overlaps_fixed_event,
    resolve_authoritative_title,
    validate_block,
    validate_bucket_adjustment,
)
from timeblock_agent.diff_write import decode_block_meta
from timeblock_agent.eventkit_bridge import CalendarEvent, Reminder
from timeblock_agent.goals import Goal

logger = logging.getLogger("timeblock_agent.incremental_replan")

RESOLUTIONS = ("completed", "running_behind", "unexpected_plan")

_SYSTEM_TEMPLATE = """\
You are the incremental replanner for a time-blocking agent. Something just happened — a \
task block ended, overran, or the user answered a check-in — and you adjust ONLY what's \
affected by it, not the whole day.

Bucket CONTAINERS themselves (e.g. "Work", not the specific task inside it) are FLEXIBLE \
— fair game to resize or move, not just the task blocks inside them. Use `bucket_adjustments` \
for that; it's a separate, independent thing from `blocks` (which only fills task content \
within a bucket's current window, whatever that window currently is after any adjustment).

`fixed_events` are real commitments already on the calendar (meetings, classes) — given \
for awareness only, and off-limits. NEVER propose a bucket_adjustments window or a task \
block that overlaps any of them, even partially. If cascading would require overlapping \
one, stop the cascade there instead and drop the lowest-priority remaining task(s) into \
`unscheduled_reminder_ids` rather than crossing it.

`other_scheduled_blocks` are today's OTHER already-placed task blocks — reminders/goal \
sessions already sitting on the calendar from an earlier pass, in this bucket or any \
other, that this cycle isn't touching. They are just as off-limits as `fixed_events`: \
never propose a block or a bucket window that overlaps one. This matters most in two \
places — extending the triggering block's own end past where a later task in the SAME \
bucket already sits, and shrinking the NEXT bucket's start (step 3 below) past where that \
bucket's OWN already-placed content starts. Either one is a real conflict, not just the \
bucket boundary itself.

The `resolution` field tells you what happened to `triggering_block_id`:
- "completed": it finished on time, or the user says they're done with it. Nothing to \
reschedule for that block itself; just make sure whatever comes later still makes sense.
- "running_behind": the user is continuing that same task longer than planned. Work \
through these in order:
  1. Extend the triggering block's own end time by roughly \
{followup_delay_continuing_minutes} minutes (the cadence the next check-in runs on), \
capped by whatever slack its bucket currently has left. Do this even when there's no \
later task block in the same bucket to cascade — extension doesn't depend on there being \
something else to push. This MUST be the exact same source and source_id as before \
(given as `triggering_source`/`triggering_source_id` below) — you are continuing the same \
task longer, not replacing it with a different reminder or goal. Only change its END — \
its start never moves. The triggering block's own CURRENT start/end are given below as \
`triggering_task_current_start`/`triggering_task_current_end` — this is the ONLY block \
whose timing this step is about; the bucket's own start/end in `flexible_blocks` is a \
DIFFERENT, wider window and must never be used as this block's start. The new block's \
`start` MUST be EXACTLY `triggering_task_current_start`, unchanged, and its `end` is \
always computed as `triggering_task_current_end` + roughly \
{followup_delay_continuing_minutes} minutes, full stop. `now` (given below) is ONLY for \
judging `hard_cutoff` and how much room is realistically left today — it is NEVER a new \
start or end boundary for anything, and never a value to compute the new end from.
  2. If step 1's new end would now land ON TOP of another already-placed task in the \
SAME bucket (check `other_scheduled_blocks` for anything with this bucket's own \
`bucket_event_id`) — this is a DIFFERENT situation from the bucket's own boundary, and \
must be handled BEFORE step 3 below even applies: that sibling task is also in the way, \
not just the bucket's edge. Re-propose THAT task too, as an ADDITIONAL entry in `blocks` \
— same source/source_id/title as it already has (never invent a new one), same \
duration, just pushed later to start right after the triggering block's new end (plus \
roughly `{buffer_block_minutes}` minutes). If pushing it collides with yet another \
sibling further out, cascade through that one the same way, recursively, in whatever \
chronological order they already sit. Only once every same-bucket sibling in the way has \
either been pushed clear or dropped (see below) do you move on to whether the bucket's \
own boundary (step 3) also needs extending.
  3. If the bucket's slack is already exhausted (its end already matches the block's end) \
and the task still needs room, propose a `bucket_adjustments` entry extending that \
bucket's own end by roughly the same amount, THEN extend the block into that new room. \
Treat this as a last resort, only once step 1's existing slack is genuinely gone — and \
never propose a bucket_adjustments end past `hard_cutoff` given below. Same rule as \
above: a bucket_adjustments entry's `new_start` must be EXACTLY the bucket's own current \
start (given below, in `flexible_blocks`) — you are only ever extending its end forward, \
never moving its start, never using `now` as the new_start. IMPORTANT: check this \
extension's new END against the very next bucket's own current start BEFORE finalizing \
this response — if it would land ON OR PAST that next bucket's start, step 4 below is \
NOT optional: this extension alone WILL be rejected as an overlap unless you ALSO include \
a matching adjustment for that next bucket (pushed later, or shrunk-from-front — see \
step 4) as a SECOND entry in the SAME `bucket_adjustments` list, in this SAME response. \
Never submit the overrunning bucket's own extension by itself when it reaches that far. \
If `overrun_pairing_hint` is present below (non-null), this situation already applies \
this cycle — it gives you two COMPLETE, self-contained scenarios so there's no \
arithmetic left to do: judge priority, then use ONLY the one matching scenario's numbers \
— `if_next_bucket_equal_or_higher_priority` (giving `overrunning_bucket_new_end` for step \
3's own extension AND `next_bucket_new_start`/`next_bucket_new_end` for step 4's entry), \
OR `if_next_bucket_lower_priority` (same three fields, the shrink numbers instead). \
NEVER mix a value from one scenario with a value from the other — each one's three \
numbers already agree with each other exactly as given; combining across them (e.g. the \
higher-priority scenario's own extension with the lower-priority scenario's next-bucket \
numbers) will leave a real gap and get rejected. Copy all three numbers from whichever \
ONE scenario you pick, verbatim. You still decide which scenario applies, and you must \
still submit BOTH entries (`overrunning_bucket_event_id` and `next_bucket_event_id` from \
the hint) together in \
one `bucket_adjustments` list — the hint only removes the arithmetic, not the decision \
or the requirement to submit both.
  4. If extending a bucket's end (from step 3) would now overlap the very next \
bucket's start, you have a genuine priority decision to make — the user has a finite \
number of hours today, and something has to give. Judge the relative importance of the \
overrunning bucket vs. the next one from their titles, `active_goal_titles` (what the \
user is actively working toward right now), and — most importantly — the user's own \
bucket descriptions below, if they say anything specific about either bucket:

{bucket_context}

Trust the descriptions above over guessing from title-shape when they say something \
specific. Absent any specific guidance, work-shaped/obligation-like buckets generally \
outrank leisure-shaped ones, but use judgment, not a fixed rule — this changes over time \
(e.g. once school work exists alongside a job) and is exactly why none of this is a \
hardcoded setting.
     - Unconditional exception, checked BEFORE any judgment call: if the next bucket's \
`sourced_from_bucket_event_id` equals the overrunning bucket's own event_id, it is \
ALWAYS lower priority, full stop — its time was borrowed from the overrunning bucket in \
the first place (a "Goal Time" bucket carved from it earlier today), so it gets reclaimed \
first, before touching anything else. No title/goal comparison needed for this case.
     - If the NEXT bucket is equal or higher priority than the one overrunning: push it \
later instead, same duration, preserving its full time — cascade further to whatever \
comes after that the same way, still never past `hard_cutoff`.
     - If the NEXT bucket is lower priority: let the overrun eat directly into its time \
instead of delaying it — propose a `bucket_adjustments` entry with `new_end` EXACTLY that \
bucket's own current end (unchanged) and `new_start` pushed later to where the overrun \
now ends. This is a distinct, fully supported shape from step 3's extension — the two \
never mix in one entry: step 3 keeps `new_start` fixed and grows `new_end`; this one keeps \
`new_end` fixed and grows `new_start`. Its total time today shrinks; that's the intended \
trade-off, not a bug. Only shrink it down to `{min_block_minutes}` minutes minimum — if \
honoring the full overrun would shrink it below that, split the difference: shrink it to \
the minimum and push whatever's left of the overrun onto whatever comes after that (same \
priority judgment, recursively). Check the new `new_start` against that bucket's own \
`other_scheduled_blocks` too — don't push the encroaching overrun past wherever that \
bucket's own content already begins.
  5. If honoring the cascade would push past `hard_cutoff` (step 4), OR a same-bucket \
sibling from step 2 genuinely can't be pushed clear within the bucket's own window even \
after step 3's extension, stop there: drop the lowest-priority remaining task(s) from \
being scheduled today instead of crossing it — put them in `unscheduled_reminder_ids` \
(they get reconsidered fresh tomorrow, not lost).
- "unexpected_plan": something else came up and displaced the block. Treat its remaining \
time as open again for re-filling from Reminders; only cascade later blocks/buckets \
forward (same mechanism as above) if there genuinely isn't room otherwise.

Rules:
- Only touch the triggering block/bucket and whatever must shift because of it. Leave \
every other block or bucket exactly as-is by omitting it from your output — omission \
means "don't change this," not "delete this."
- For "running_behind" specifically, the entry starting EXACTLY at \
`triggering_task_current_start` must never substitute a different reminder/goal — same \
source and source_id as `triggering_source`/`triggering_source_id`, always, only the times \
change. Other entries in the same bucket (a sibling being cascaded per step 2) are a \
separate matter and keep THEIR OWN existing source/source_id, never the triggering one's.
- Every task block must fit inside its own bucket's CURRENT window (after any \
bucket_adjustments you're also proposing this cycle) and be at least {min_block_minutes} \
minutes long. Leave roughly {buffer_block_minutes} minutes between back-to-back blocks in \
the same bucket when there's slack to do so.
- `allow_goal_fill` is only ever true for protected buffers — never place a goal-fill \
block anywhere else, and never more than one across the whole day. Never invent a \
goal-fill block at all if no `goal_candidate` is given in this message.
- Never propose anything (block or bucket_adjustments) starting before `now`, and never a \
bucket_adjustments new_end past `hard_cutoff`.

Always call the propose_incremental_layout tool exactly once with your full result.
"""

_INCREMENTAL_TOOL = {
    "name": "propose_incremental_layout",
    "description": "Adjust only the affected portion of today's remaining FLEXIBLE_BUCKET blocks.",
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
            "unscheduled_reminder_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["blocks", "bucket_adjustments", "unscheduled_reminder_ids"],
    },
}


@dataclass
class IncrementalResult:
    blocks: list[ProposedBlock]
    bucket_adjustments: list[BucketAdjustment]
    unscheduled_reminder_ids: list[str]


def _build_system_prompt(rules: Rules, bucket_context: str) -> str:
    return _SYSTEM_TEMPLATE.format(
        min_block_minutes=rules.min_block_minutes,
        buffer_block_minutes=rules.buffer_block_minutes,
        followup_delay_continuing_minutes=rules.followup_delay_continuing_minutes,
        bucket_context=bucket_context or "(none configured yet — use judgment from titles/goals alone)",
    )


def _compute_overrun_pairing_hint(
    resolution: str,
    triggering_block_id: str,
    triggering_block_end: Optional[datetime],
    bucket_blocks: list[CalendarEvent],
    rules: Rules,
    hard_cutoff: datetime,
) -> Optional[dict]:
    """Precomputes the exact candidate numbers for BOTH halves of rule 4's cascade —
    the overrunning bucket's own extension AND its next-bucket counterpart (whichever
    shape ends up applying) — rather than leaving the model to derive them itself.

    Caught live: even after the prompt explicitly warned that an extension reaching the
    next bucket's start requires a paired adjustment in the SAME response, 4 straight
    live cycles still produced either no attempt at all, an extension with no pairing, or
    (once) an extension straight to `hard_cutoff` — never the correct pair. Handing over
    ready-made numbers removes the arithmetic from the model's job entirely; it only needs
    to (a) decide the extension is actually needed and (b) judge which of the two given
    shapes fits, then copy the corresponding numbers verbatim into both entries. If this
    still isn't reliable, the next escalation is a dedicated follow-up call scoped to just
    the priority judgment — not attempted yet, tried this first as it's cheaper (still one
    API call) and keeps the model doing its own arithmetic nowhere in this path.

    Returns None when no cascade is actually imminent this cycle (there's still natural
    slack, or nothing sits immediately next) — nothing useful to hint at."""
    if resolution != "running_behind" or triggering_block_end is None:
        return None
    bucket = next((b for b in bucket_blocks if b.identifier == triggering_block_id), None)
    if bucket is None:
        return None
    candidate_end = triggering_block_end + timedelta(minutes=rules.followup_delay_continuing_minutes)
    candidate_end = min(candidate_end, hard_cutoff)
    if candidate_end <= bucket.end:
        return None  # natural slack still covers it — no bucket_adjustment needed yet
    upcoming = sorted(
        (b for b in bucket_blocks if b.identifier != bucket.identifier and b.start >= bucket.end),
        key=lambda b: b.start,
    )
    next_bucket = upcoming[0] if upcoming else None
    if next_bucket is None or candidate_end <= next_bucket.start:
        return None  # plain extension fits with nothing else in the way — no pairing needed
    overlap = candidate_end - next_bucket.start
    push_later_start = next_bucket.start + overlap
    push_later_end = next_bucket.end + overlap
    # The shrink shape is floored at min_block_minutes, which can cap it BELOW the full
    # `candidate_end` ask — caught live: giving the model one shared "overrunning bucket's
    # own new end" number regardless of which shape it picks meant the shrink scenario got
    # a mismatched pair (Test Work told to extend to the full, uncapped candidate_end,
    # Test Hobby told to shrink only to the floored point) — internally inconsistent, left
    # a real gap between the two, rejected as an overlap. The overrunning bucket can only
    # ever extend as far as wherever the next bucket's (possibly floored) start ends up in
    # THIS scenario, never further — each scenario below is now fully self-contained, with
    # its own matching pair of numbers, so there's no shared value to mismatch.
    shrink_next_start = min(candidate_end, next_bucket.end - timedelta(minutes=rules.min_block_minutes))
    return {
        "overrunning_bucket_event_id": bucket.identifier,
        "next_bucket_event_id": next_bucket.identifier,
        "if_next_bucket_equal_or_higher_priority": {
            "overrunning_bucket_new_end": candidate_end.isoformat(),
            "next_bucket_new_start": push_later_start.isoformat(),
            "next_bucket_new_end": push_later_end.isoformat(),
        },
        "if_next_bucket_lower_priority": {
            "overrunning_bucket_new_end": shrink_next_start.isoformat(),
            "next_bucket_new_start": shrink_next_start.isoformat(),
            "next_bucket_new_end": next_bucket.end.isoformat(),
        },
    }


def _find_goal_time_sourced_from(bucket_event_id: str, bucket_blocks: list[CalendarEvent]) -> Optional[CalendarEvent]:
    return next(
        (b for b in bucket_blocks if b.identifier != bucket_event_id and decode_goal_time_meta(b.notes) == bucket_event_id),
        None,
    )


def _compute_self_sourced_goal_time_correction(
    resolution: str,
    triggering_block_id: str,
    triggering_block_end: Optional[datetime],
    bucket_blocks: list[CalendarEvent],
    rules: Rules,
    hard_cutoff: datetime,
) -> Optional[BucketAdjustment]:
    """A synthetic "Goal Time" bucket carved from the SAME bucket that's now overrunning
    must ALWAYS shrink to give that borrowed time back — this is an unconditional rule
    (rules.md §5: "it's really Work's own budget on loan... reclaims it before touching
    anyone else's time"), not a priority judgment call, so it's enforced deterministically
    here in code rather than left to the model to get right every time.

    Caught live: Goal Time sits NESTED WITHIN its donor bucket's own span (carved from
    natural slack, not appended after it), so `_compute_overrun_pairing_hint`'s "next
    bucket after this one ends" logic never even applies here — and, unprompted, the
    model instead proposed PUSHING Goal Time later (preserving its full duration) rather
    than shrinking it. Push-later is a real, legitimate shape elsewhere (rule 4's first
    branch, a genuine judgment call) — just never correct for THIS specific, unconditional
    case. Rather than hope the model reliably picks shrink over push here too, this
    computes and returns the correct shrink adjustment directly, overriding whatever (if
    anything) the model itself proposed for this same bucket.

    Returns None when Goal Time isn't even in the way yet (natural slack still covers the
    triggering block's continuation) or it's already at its own floor (nothing further to
    give up — a genuinely maxed-out cascade, not this function's job to resolve further).

    NOTE: this only shrinks the Goal Time BUCKET CONTAINER — the actual goal-session TASK
    BLOCK filling its front also needs correcting to match, or it's left sitting partially
    before the bucket's new start (caught live: the triggering block's own extension then
    collided with that stale task block every cycle, even though the container itself was
    already correctly shrunk). See `_find_goal_session_front_segment` for that half."""
    if resolution != "running_behind" or triggering_block_end is None:
        return None
    triggering_bucket = next((b for b in bucket_blocks if b.identifier == triggering_block_id), None)
    if triggering_bucket is None:
        return None
    goal_time = _find_goal_time_sourced_from(triggering_block_id, bucket_blocks)
    if goal_time is None:
        return None
    candidate_end = triggering_block_end + timedelta(minutes=rules.followup_delay_continuing_minutes)
    candidate_end = min(candidate_end, hard_cutoff, triggering_bucket.end)
    if candidate_end <= goal_time.start:
        return None  # natural slack still covers it — Goal Time isn't in the way yet
    new_start = min(candidate_end, goal_time.end - timedelta(minutes=rules.min_block_minutes))
    if new_start <= goal_time.start:
        return None  # already at (or past) its own floor — nothing further to give up
    return BucketAdjustment(bucket_event_id=goal_time.identifier, new_start=new_start, new_end=goal_time.end)


def _find_goal_session_front_segment(
    goal_time_current_start: datetime,
    bucket_event_id: str,
    other_agent_blocks: list[CalendarEvent],
) -> Optional[CalendarEvent]:
    """Finds a goal-session task block for this Goal Time bucket whose own start no
    longer matches the bucket's CURRENT front — deliberately NOT tied to whether the
    bucket container is ALSO being shrunk THIS specific cycle. Caught live: the bucket
    container was correctly shrunk once (an earlier cycle), but the task block filling
    it — a separate calendar event that never moves on its own — was left stale from
    THAT correction, since at the time this check only ran piggybacked on a fresh
    same-cycle shrink. A later cycle where the bucket needs no FURTHER shrinking (it's
    already caught up) would then never revisit the task block either, leaving it
    permanently stale. This is now a standalone consistency check: any mismatch between
    the task block's start and the bucket's current front — however it arose — gets
    corrected, every cycle. A LATER segment (multi-goal split — see rules.md §2) already
    starts after the bucket's front and is untouched, so only ever at most one match here.
    Returns None if the correction would eat into or past the segment's own end (a bigger
    overrun than a simple front-correction can handle — left as a known limit)."""
    for ev in other_agent_blocks:
        meta = decode_block_meta(ev.notes)
        if meta is None or meta.get("bucket_event_id") != bucket_event_id:
            continue
        if ev.start >= goal_time_current_start:
            continue  # already matches the bucket's current front, or starts later (not the front segment)
        if goal_time_current_start >= ev.end:
            return None
        return ev
    return None


def _build_user_payload(
    now: datetime,
    resolution: str,
    triggering_block_id: str,
    bucket_blocks: list[CalendarEvent],
    reminders: list[Reminder],
    rules: Rules,
    goal_candidate: Optional[Goal],
    hard_cutoff: datetime,
    fixed_events: list[CalendarEvent],
    triggering_source: Optional[str],
    triggering_source_id: Optional[str],
    active_goal_titles: list[str],
    triggering_block_start: Optional[datetime],
    triggering_block_end: Optional[datetime],
    other_agent_blocks: list[CalendarEvent],
    overrun_pairing_hint: Optional[dict],
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
                "sourced_from_bucket_event_id": decode_goal_time_meta(ev.notes),
            }
        )

    fixed_payload = [
        {"title": ev.title, "start": ev.start.isoformat(), "end": ev.end.isoformat()} for ev in fixed_events
    ]

    other_scheduled_payload = []
    for ev in other_agent_blocks:
        meta = decode_block_meta(ev.notes) or {}
        other_scheduled_payload.append(
            {
                "bucket_event_id": meta.get("bucket_event_id"),
                "title": ev.title,
                "start": ev.start.isoformat(),
                "end": ev.end.isoformat(),
            }
        )

    reminders_payload = [
        {
            "reminder_id": r.identifier,
            "title": r.title,
            "due_date": r.due_date.date().isoformat() if r.due_date else None,
            "priority": r.priority,
        }
        for r in reminders
    ]

    goal_payload = None
    if goal_candidate is not None:
        goal_payload = {"title": goal_candidate.title, "next_action": goal_candidate.next_action}

    return {
        "now": now.isoformat(),
        "resolution": resolution,
        "triggering_block_id": triggering_block_id,
        "triggering_source": triggering_source,
        "triggering_source_id": triggering_source_id,
        "triggering_task_current_start": triggering_block_start.isoformat() if triggering_block_start else None,
        "triggering_task_current_end": triggering_block_end.isoformat() if triggering_block_end else None,
        "flexible_blocks": flexible_payload,
        "fixed_events": fixed_payload,
        "other_scheduled_blocks": other_scheduled_payload,
        "open_reminders": reminders_payload,
        "goal_candidate": goal_payload,
        "active_goal_titles": active_goal_titles,
        "hard_cutoff": hard_cutoff.isoformat(),
        "overrun_pairing_hint": overrun_pairing_hint,
    }


def _clamp_triggering_extension(
    block: ProposedBlock,
    trigger_end: datetime,
    bucket: Optional[CalendarEvent],
    fixed_events: list[CalendarEvent],
    other_events: list[CalendarEvent],
) -> ProposedBlock:
    """Caps a "running_behind" continuation's proposed end at the nearest real conflict —
    a fixed event, another already-placed block, or the bucket's own current end — sitting
    between the block's current end and the model's naive `current_end + N minutes`
    proposal, instead of relying on the model to have accounted for it itself.

    Caught live: Test Meeting sat nested inside Test Work, between beta's current end and
    where the plain formula would land; the model has no instruction anywhere telling it
    to stop short of a mid-bucket fixed event specifically (only a generic "never overlap
    fixed_events" at the top, which a small model doesn't reliably combine with the
    separate "+N minutes" formula) — it proposed running straight through the meeting
    every single cycle, which then failed the existing hard fixed-event-overlap check
    every single cycle, making "still going" completely inert for the rest of the day
    once a meeting was in the way. Same category as the earlier "0 proposed blocks, model
    used `now` as the new start" bug, and the same fix philosophy as day_layout's
    squash-correction (bug #9): don't just reject an invalid proposal when a valid,
    strictly-better one is trivially computable from data already known — extend as far
    as genuinely possible instead of not at all.

    Only ever shrinks the proposed end, never grows it past what the model proposed. If
    there's no genuine room past `trigger_end` at all (already fully blocked), returns
    `block` unchanged and lets the normal validation reject it — nothing to correct to.
    """
    candidates = [block.end]
    if bucket is not None:
        candidates.append(bucket.end)
    for ev in fixed_events + other_events:
        if trigger_end < ev.start < block.end:
            candidates.append(ev.start)
    capped_end = min(candidates)
    if capped_end <= trigger_end:
        return block
    return replace(block, end=capped_end)


def replan_incremental(
    now: datetime,
    resolution: str,
    triggering_block_id: str,
    bucket_blocks: list[CalendarEvent],
    reminders: list[Reminder],
    rules: Rules,
    goal_candidate: Optional[Goal] = None,
    fixed_events: Optional[list[CalendarEvent]] = None,
    triggering_source: Optional[str] = None,
    triggering_source_id: Optional[str] = None,
    active_goal_titles: Optional[list[str]] = None,
    bucket_context: str = "",
    specific_events: Optional[list[CalendarEvent]] = None,
    triggering_block_start: Optional[datetime] = None,
    triggering_block_end: Optional[datetime] = None,
    other_agent_blocks: Optional[list[CalendarEvent]] = None,
) -> IncrementalResult:
    """`bucket_blocks` should be today's remaining FLEXIBLE_BUCKET events (the triggering
    one's bucket plus any later ones that might need to cascade). `fixed_events` are
    today's real commitments (meetings, classes) — required for the code-level overlap
    check, since the model is only ever shown them for awareness, never trusted to avoid
    them unprompted. `specific_events` (FLEXIBLE_SPECIFIC — Gym, Lunch, etc.) get the same
    protection for the same reason day_layout.plan_day needs them: not "real commitments"
    in the FIXED sense, but still already-scheduled blocks a bucket resize must never
    silently run over. `triggering_source`/`triggering_source_id` (from the resolved
    block's own agent-meta) let "running_behind" enforce that the triggering block keeps
    the same reminder/goal rather than the model silently substituting a different one.
    `triggering_block_start`/`triggering_block_end` (the triggering task's own current
    start/end) enforce the other half of that same guarantee: "running_behind" may ONLY
    move the triggering block's end forward, never its start — caught live, three times,
    with three different confusions (using `now` as a new start; then, once that was
    fixed in the prompt, using an adjacent block's old start; then discovering the model
    was never even TOLD the triggering block's own current start/end at all — only the
    bucket container's much wider window — so it had no start to anchor to besides the
    bucket's own, and kept proposing that instead every time regardless of prompt wording).
    Both values are now given directly in the payload (`triggering_task_current_start`/
    `_end`) so the model has an actual number to extend from, AND checked here in code as
    a hard backstop — matching source/source_id alone doesn't catch either failure mode.
    `other_agent_blocks` (today's OTHER already-placed agent-authored task blocks,
    excluding the triggering one itself) close a related gap caught live: the model was
    never told about other reminders already scheduled later in the same bucket (or
    already sitting in a bucket it's now cascading into), so an extension or a
    next-bucket shrink could silently run straight through one — the model has no way to
    avoid a conflict it was never shown. Required for the code-level overlap check on the
    final block, same reasoning as `fixed_events`/`specific_events`.
    `active_goal_titles` and `bucket_context` (see bucket_context.py / buckets.md) give the
    model context for judging relative bucket priority when an overrun collides with the
    next bucket — the user's own descriptions of what each bucket means/how important it
    is, trusted over guessing from title-shape alone. Raises on API failure — caller must
    no-op (leave the calendar untouched) rather than guess."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}, got {resolution!r}")
    if not bucket_blocks:
        return IncrementalResult([], [], [r.identifier for r in reminders])

    fixed_events = fixed_events or []
    specific_events = specific_events or []
    other_agent_blocks = other_agent_blocks or []
    active_goal_titles = active_goal_titles or []
    hard_cutoff = datetime.combine(now.date(), rules.evening_checkin_floor)

    client = get_client()
    system_prompt = _build_system_prompt(rules, bucket_context)
    overrun_pairing_hint = _compute_overrun_pairing_hint(
        resolution, triggering_block_id, triggering_block_end, bucket_blocks, rules, hard_cutoff,
    )
    user_payload = _build_user_payload(
        now, resolution, triggering_block_id, bucket_blocks, reminders, rules, goal_candidate,
        hard_cutoff, fixed_events, triggering_source, triggering_source_id, active_goal_titles,
        triggering_block_start, triggering_block_end, other_agent_blocks, overrun_pairing_hint,
    )

    response = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=8192,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_INCREMENTAL_TOOL],
        tool_choice={"type": "tool", "name": "propose_incremental_layout"},
        messages=[{"role": "user", "content": json.dumps(user_payload, indent=2)}],
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    buckets_by_id = {ev.identifier: ev for ev in bucket_blocks}
    reminders_by_id = {r.identifier: r for r in reminders}

    logger.info(
        "replan_incremental(%s, %s): raw model response — %d block(s), %d bucket_adjustment(s) proposed",
        resolution, triggering_block_id,
        len(tool_use.input.get("blocks", [])), len(tool_use.input.get("bucket_adjustments", [])),
    )
    for b in tool_use.input.get("blocks", []):
        logger.info(
            "  raw block: bucket_event_id=%r title=%r %s-%s source=%s/%s",
            b.get("bucket_event_id"), b.get("title"), b.get("start"), b.get("end"),
            b.get("source"), b.get("source_id"),
        )
    for a in tool_use.input.get("bucket_adjustments", []):
        logger.info(
            "  raw bucket_adjustment: bucket_event_id=%r new_start=%s new_end=%s",
            a.get("bucket_event_id"), a.get("new_start"), a.get("new_end"),
        )

    # Reminders the model attempted to reposition THIS cycle (step 2's same-bucket
    # sibling cascade) — their OLD calendar slot is stale the moment ANYTHING in that
    # same bucket succeeds this cycle: _write_incremental_result/apply_layout reconciles
    # every existing event in a touched bucket, deleting any old slot not represented in
    # the final accepted blocks. So an attempted (even if ultimately rejected) sibling's
    # old position must NOT be treated as a real conflict for other proposals this cycle
    # — caught live: beta's own legitimate extension got rejected for "overlapping"
    # gamma's old slot, even though gamma was being cascaded away in the very same
    # response. If nothing in the bucket ends up succeeding, nothing gets written at all,
    # so there's no real overlap on the calendar either way.
    attempted_ids = {
        (item.get("source"), item.get("source_id")) for item in tool_use.input.get("blocks", [])
    }
    other_agent_blocks_for_check = [
        ev for ev in other_agent_blocks
        if (m := decode_block_meta(ev.notes)) is None or (m.get("source"), m.get("source_id")) not in attempted_ids
    ]

    # Deterministic, unconditional override — computed and applied BEFORE anything the
    # model proposed even gets looked at, since this specific case (Goal Time sourced
    # from the bucket that's now overrunning) is not a judgment call at all (see the
    # function's own docstring). Applying it directly to `buckets_by_id` here means the
    # two-pass logic below, and the triggering block's own clamp later, both naturally
    # see the CORRECTED position with no further special-casing needed.
    self_sourced_correction = _compute_self_sourced_goal_time_correction(
        resolution, triggering_block_id, triggering_block_end, bucket_blocks, rules, hard_cutoff,
    )
    adjustments: list[BucketAdjustment] = []
    if self_sourced_correction is not None:
        goal_time_event = buckets_by_id.get(self_sourced_correction.bucket_event_id)
        logger.info(
            "replan_incremental: 'Goal Time' %r sourced from the overrunning bucket — "
            "unconditionally shrinking %s-%s -> %s-%s regardless of what the model proposed for it",
            goal_time_event.title if goal_time_event else self_sourced_correction.bucket_event_id,
            goal_time_event.start if goal_time_event else "?", goal_time_event.end if goal_time_event else "?",
            self_sourced_correction.new_start, self_sourced_correction.new_end,
        )
        adjustments.append(self_sourced_correction)
        buckets_by_id[self_sourced_correction.bucket_event_id] = replace(
            buckets_by_id[self_sourced_correction.bucket_event_id],
            start=self_sourced_correction.new_start, end=self_sourced_correction.new_end,
        )

    # Independent of whether the bucket container needed a NEW shrink this cycle — a
    # goal-session TASK BLOCK is a separate calendar event that never moves on its own,
    # so it can be left stale from an EARLIER cycle's shrink even once the container
    # itself has fully caught up (caught live: this was originally piggybacked on
    # `self_sourced_correction` firing THIS cycle, which meant once the bucket no longer
    # needed further shrinking, the stale task block from a prior cycle's shrink was
    # never revisited either, and kept blocking the triggering block's own extension
    # forever). Checked every running_behind cycle regardless.
    goal_session_correction: Optional[ProposedBlock] = None
    stale_goal_session_id: Optional[str] = None
    if resolution == "running_behind":
        goal_time_ref = _find_goal_time_sourced_from(triggering_block_id, bucket_blocks)
        if goal_time_ref is not None:
            current_goal_time = buckets_by_id.get(goal_time_ref.identifier, goal_time_ref)
            stale_segment = _find_goal_session_front_segment(
                current_goal_time.start, goal_time_ref.identifier, other_agent_blocks,
            )
            if stale_segment is not None:
                meta = decode_block_meta(stale_segment.notes) or {}
                goal_session_correction = ProposedBlock(
                    bucket_event_id=goal_time_ref.identifier, title=stale_segment.title,
                    start=current_goal_time.start, end=stale_segment.end,
                    source=meta.get("source", "goal"), source_id=meta.get("source_id", "goal"),
                )
                stale_goal_session_id = stale_segment.identifier
                logger.info(
                    "replan_incremental: correcting stale goal-session block %r to match its "
                    "bucket's current front: %s-%s -> %s-%s",
                    stale_segment.title, stale_segment.start, stale_segment.end,
                    goal_session_correction.start, goal_session_correction.end,
                )
    if stale_goal_session_id is not None:
        # Its old position is about to be superseded by goal_session_correction (added to
        # `blocks` below) regardless of anything the model proposed — same reasoning as
        # the stale-sibling exclusion above, just for a correction WE computed rather than
        # one the model attempted.
        other_agent_blocks_for_check = [ev for ev in other_agent_blocks_for_check if ev.identifier != stale_goal_session_id]

    # Bucket resizes are validated and applied to a local copy of buckets_by_id FIRST, so
    # that block validation below checks fit against each bucket's post-adjustment window
    # rather than its original one.
    #
    # Two-pass, not one: a single forward pass over the raw list can't correctly
    # validate two MUTUALLY-dependent adjustments in the same response (e.g. step 3's
    # cascade — bucket A extends its end to X while bucket B's start is ALSO pushed to X
    # in the same batch) no matter which order the model happens to list them in —
    # whichever is checked first only ever sees the OTHER one's stale, not-yet-applied
    # window, so a perfectly consistent pair gets wrongly rejected either way (caught
    # live). Pass 1 builds a hypothetical bucket state with EVERY raw proposal applied
    # optimistically (bogus ones just produce a hypothetical entry that later fails its
    # own shape/overlap check in pass 2 and never reaches the real buckets_by_id). Pass 2
    # validates each adjustment for real, using every OTHER bucket's fully-hypothetical
    # (both-applied) position for the cross-bucket overlap check, while still checking
    # shape (start/end match) against the bucket's own real, current-before-this-cycle
    # state in buckets_by_id.
    hypothetical_buckets = dict(buckets_by_id)
    for item in tool_use.input.get("bucket_adjustments", []):
        # Never let the model's own (possibly wrong) proposal for the unconditionally-
        # overridden bucket clobber the correction already folded into buckets_by_id above.
        if self_sourced_correction is not None and item["bucket_event_id"] == self_sourced_correction.bucket_event_id:
            continue
        bucket = buckets_by_id.get(item["bucket_event_id"])
        if bucket is not None:
            hypothetical_buckets[item["bucket_event_id"]] = replace(
                bucket, start=datetime.fromisoformat(item["new_start"]), end=datetime.fromisoformat(item["new_end"])
            )

    for item in tool_use.input.get("bucket_adjustments", []):
        if self_sourced_correction is not None and item["bucket_event_id"] == self_sourced_correction.bucket_event_id:
            logger.info(
                "replan_incremental: ignoring the model's own proposal for %r — already "
                "handled by the unconditional Goal-Time-shrink override above",
                item["bucket_event_id"],
            )
            continue
        adjustment = BucketAdjustment(
            bucket_event_id=item["bucket_event_id"],
            new_start=datetime.fromisoformat(item["new_start"]),
            new_end=datetime.fromisoformat(item["new_end"]),
        )
        other_bucket_events = (
            specific_events
            + [b for eid, b in hypothetical_buckets.items() if eid != adjustment.bucket_event_id]
            + other_agent_blocks_for_check
        )
        if not validate_bucket_adjustment(
            adjustment, buckets_by_id, hard_cutoff, fixed_events, other_bucket_events,
            min_block_minutes=rules.min_block_minutes,
        ):
            bucket_event = buckets_by_id.get(adjustment.bucket_event_id)
            logger.warning(
                "replan_incremental: rejected bucket_adjustment for %r (%s-%s -> %s-%s) — failed validate_bucket_adjustment",
                bucket_event.title if bucket_event else adjustment.bucket_event_id,
                bucket_event.start if bucket_event else "?", bucket_event.end if bucket_event else "?",
                adjustment.new_start, adjustment.new_end,
            )
            continue
        adjustments.append(adjustment)
        original = buckets_by_id[adjustment.bucket_event_id]
        buckets_by_id[adjustment.bucket_event_id] = replace(
            original, start=adjustment.new_start, end=adjustment.new_end
        )

    blocks: list[ProposedBlock] = []
    if goal_session_correction is not None:
        blocks.append(goal_session_correction)
    for item in tool_use.input["blocks"]:
        if (
            goal_session_correction is not None
            and item["bucket_event_id"] == goal_session_correction.bucket_event_id
            and item["source"] == goal_session_correction.source
            and item["source_id"] == goal_session_correction.source_id
        ):
            # Same reasoning as the bucket_adjustments skip above: this exact goal-session
            # block is already handled by the deterministic override, so the model's own
            # raw proposal for it (if any) is ignored rather than allowed to silently
            # overwrite the correction with a second, possibly wrong, entry sharing the
            # same (bucket_event_id, source, source_id) key.
            logger.info(
                "replan_incremental: ignoring the model's own proposal for goal-session block %r — "
                "already handled by the unconditional Goal-Time-shrink override above",
                item.get("title"),
            )
            continue
        block = ProposedBlock(
            bucket_event_id=item["bucket_event_id"],
            title=item["title"],
            start=datetime.fromisoformat(item["start"]),
            end=datetime.fromisoformat(item["end"]),
            source=item["source"],
            source_id=item["source_id"],
        )

        # Enforced in code, not just prompt text: whichever entry claims the triggering
        # block's own start time must continue the SAME reminder/goal, never silently
        # substitute another. Scoped to `block.start == triggering_block_start` (not just
        # "any entry in the same bucket") since step 2 of the prompt now legitimately
        # allows OTHER same-bucket siblings to also be re-proposed (cascaded later) with
        # their own, different source_id — a blanket per-bucket check would wrongly
        # reject every one of those (caught live: a sibling's legitimate cascade got
        # rejected as "substitution" even though it never touched the trigger's own slot).
        if (
            resolution == "running_behind"
            and triggering_block_start is not None
            and block.start == triggering_block_start
            and triggering_source is not None
            and triggering_source_id is not None
            and (block.source, block.source_id) != (triggering_source, triggering_source_id)
        ):
            logger.warning(
                "replan_incremental: rejected proposed block %r (%s-%s) — running_behind tried to "
                "substitute source %r/%r instead of the triggering %r/%r",
                item.get("title"), block.start, block.end, block.source, block.source_id,
                triggering_source, triggering_source_id,
            )
            continue

        # The other half of the same guarantee, caught live: matching source/source_id
        # alone doesn't stop the model from ALSO moving the triggering block's start —
        # "running_behind" may only extend the END forward, never touch the start (seen
        # live: it moved the start back to "now", then — after that was fixed in the
        # prompt — moved it back to an adjacent block's old start instead). Prompt
        # wording alone wasn't enough either time; this is the hard backstop.
        if (
            resolution == "running_behind"
            and triggering_block_start is not None
            and triggering_source is not None
            and triggering_source_id is not None
            and (block.source, block.source_id) == (triggering_source, triggering_source_id)
            and block.start != triggering_block_start
        ):
            logger.warning(
                "replan_incremental: rejected proposed block %r (%s-%s) — running_behind moved the "
                "triggering block's own start from %s to %s; only the end may move",
                item.get("title"), block.start, block.end, triggering_block_start, block.start,
            )
            continue

        resolved_title = resolve_authoritative_title(block, reminders_by_id, goal_candidate)
        if resolved_title is None:
            logger.warning(
                "replan_incremental: rejected proposed block %r (%s-%s) — source %r/%r didn't resolve to a real reminder/goal",
                item.get("title"), block.start, block.end, block.source, block.source_id,
            )
            continue  # claimed source doesn't actually resolve — reject rather than trust it
        block.title = resolved_title

        # This item IS the triggering continuation itself (passed both checks above: the
        # right start AND the right source/id) — clamp its proposed end at the nearest
        # real conflict before running the normal checks, rather than let a naive
        # over-long proposal get hard-rejected outright and leave the block unextended.
        # Also the ONE legitimate case where a block's start is allowed to be before
        # `now`: a "still going" continuation deliberately keeps the task's own original
        # (necessarily already-past) start — only its end moves — unlike every other
        # block validate_block sees, which is always a genuinely NEW placement. Caught
        # live (TESTING_LOG.md Session 5): the blanket now-check added for Scenario 11's
        # bug initially broke this exact case, rejecting every real continuation outright.
        is_triggering_continuation = (
            resolution == "running_behind"
            and triggering_block_start is not None
            and triggering_block_end is not None
            and block.start == triggering_block_start
            and triggering_source is not None
            and triggering_source_id is not None
            and (block.source, block.source_id) == (triggering_source, triggering_source_id)
        )
        if is_triggering_continuation:
            # Goal Time (if any) sits NESTED inside this same bucket's own span, not
            # sequentially after it — buckets_by_id.get(block.bucket_event_id).end alone
            # (the bucket's OUTER boundary) wouldn't stop the clamp at Goal Time's own
            # (already corrected, above) start, so it's passed explicitly here too.
            clamp_obstacles = other_agent_blocks_for_check + blocks
            if self_sourced_correction is not None:
                clamp_obstacles = clamp_obstacles + [buckets_by_id[self_sourced_correction.bucket_event_id]]
            clamped = _clamp_triggering_extension(
                block, triggering_block_end, buckets_by_id.get(block.bucket_event_id),
                fixed_events, clamp_obstacles,
            )
            if clamped.end != block.end:
                logger.info(
                    "replan_incremental: capped triggering continuation %r's proposed end "
                    "%s down to %s — a real conflict sat in between, extending as far as "
                    "genuinely possible instead of rejecting the whole extension",
                    block.title, block.end, clamped.end,
                )
                block = clamped

        # Code-level backstop mirroring the fixed_events check inside validate_block:
        # the model is only ever shown other_agent_blocks for awareness (see
        # other_scheduled_blocks in the prompt/payload), never trusted to avoid them
        # unprompted — caught live, an extension ran straight through another reminder
        # already scheduled later in the same bucket, since nothing checked for it. Also
        # checked against `blocks` accumulated so far THIS cycle — now that step 2 of the
        # prompt allows the model to cascade multiple same-bucket siblings in one
        # response (not just the trigger), those can in principle overlap each other too.
        # This check is necessarily order-dependent (only catches overlap against
        # whichever siblings were already accepted earlier in the model's own list) —
        # accepted as a pragmatic best-effort, same as plan_day's lack of any equivalent
        # in-batch check today.
        if overlaps_fixed_event(block.start, block.end, other_agent_blocks_for_check) or overlaps_fixed_event(block.start, block.end, blocks):
            logger.warning(
                "replan_incremental: rejected proposed block %r (%s-%s) — overlaps another "
                "already-scheduled or already-accepted block this cycle",
                block.title, block.start, block.end,
            )
            continue

        validate_now = block.start if is_triggering_continuation else now
        if validate_block(block, buckets_by_id, rules, validate_now, goal_candidate_given=goal_candidate is not None, fixed_events=fixed_events):
            blocks.append(block)
        else:
            bucket_event = buckets_by_id.get(block.bucket_event_id)
            logger.warning(
                "replan_incremental: rejected proposed block %r (%s-%s, source=%s/%s) — failed validate_block "
                "against bucket %r (%s-%s)",
                block.title, block.start, block.end, block.source, block.source_id,
                bucket_event.title if bucket_event else block.bucket_event_id,
                bucket_event.start if bucket_event else "?", bucket_event.end if bucket_event else "?",
            )

    unscheduled = list(tool_use.input["unscheduled_reminder_ids"])

    # Safety net, narrower than day_layout.plan_day's equivalent (bug #4 there): only
    # reconcile reminders the model's OWN raw proposal actually attempted to place this
    # cycle — NOT every reminder in `reminders`, since replan_incremental is deliberately
    # narrow-scoped (most reminders in that full list were never meant to be touched this
    # cycle at all; reconciling all of them would incorrectly flag most of today's
    # reminders as newly "unscheduled" on every single incremental call). Caught live: a
    # reminder (a different one than the trigger) that the model tried to reposition
    # within the triggering bucket, got correctly rejected by the checks above, then
    # vanished with no trace — not placed, not reported unscheduled, since the model
    # believed it had handled it. Mirrors plan_day's bug #4 fix, scoped correctly for
    # this function's narrower contract.
    attempted_reminder_ids = {
        item["source_id"] for item in tool_use.input["blocks"] if item.get("source") == "reminder"
    }
    placed_reminder_ids = {b.source_id for b in blocks if b.source == "reminder"}
    for reminder_id in attempted_reminder_ids:
        if reminder_id not in placed_reminder_ids and reminder_id not in unscheduled:
            title = reminders_by_id[reminder_id].title if reminder_id in reminders_by_id else reminder_id
            logger.warning(
                "replan_incremental: reminder %r was attempted but rejected, and not reported as "
                "unscheduled by the model — added to unscheduled_reminder_ids in code",
                title,
            )
            unscheduled.append(reminder_id)

    return IncrementalResult(blocks=blocks, bucket_adjustments=adjustments, unscheduled_reminder_ids=unscheduled)
