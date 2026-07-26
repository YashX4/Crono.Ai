"""Per-cycle calendar event classification (see spec: "Fixed vs Flexible classification").

Three-way, judged fresh from context every cycle rather than any static config, since the
shape of a day's blocks changes (e.g. summer vs. school semester) and shouldn't require a
rules.yaml edit to keep up:

  FIXED             - meetings, classes, anything with attendees/location/commitment
                      language. Never moved, edited, or deleted.
  FLEXIBLE_BUCKET    - a generic/open-ended container that should be filled with a specific
                       task pulled from Reminders or a goal (e.g. "Work", "HobbyMaxxing").
  FLEXIBLE_SPECIFIC  - already names a concrete, self-contained activity (e.g. "Gym",
                       "Lunch"). Movable/resizable during a reshuffle, never filled/replaced.

Ambiguous FIXED-vs-FLEXIBLE defaults to FIXED; ambiguous bucket-vs-specific defaults to
FLEXIBLE_SPECIFIC — safer to leave a block's content alone than to overwrite it.

Bucket-vs-specific judgment is informed by `bucket_context` (see bucket_context.py /
buckets.md) — the user's own freeform description of what a "bucket" means to them and
their current buckets by name/spirit, not a fixed keyword list. This is deliberate: a
keyword list breaks the moment a bucket gets renamed (e.g. "Work" splitting into "Project
Time"/"Classwork" once school starts), while a model reading a description generalizes.

API failure fallback per decisions log: no-op. Callers should catch exceptions from
classify_events and treat every event as FIXED (leave the calendar untouched) rather than guess.

Pre-call diff check (cost guardrail, see spec's "Cost & Budget"): classify_events() is
re-invoked on essentially every scheduler tick (run_scheduled_tick, handle_day_boundary,
run_checkin_answer all call it), usually classifying the exact same events it just
classified minutes ago — plan_day/replan_incremental don't have this problem (they only
ever run in response to a genuine trigger — day-start once, or a real check-in answer —
so there's nothing redundant to skip there). A small in-memory cache, keyed on every
field that could actually change the answer (the event's own fields plus a hash of
bucket_context, so editing buckets.md correctly invalidates rather than silently serving
a stale classification), means a tick where nothing changed skips the Claude call
entirely; a tick where only one event changed only sends that one. Cleared at day-start
for a clean slate and to keep it from growing unbounded across a long-running process.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from timeblock_agent.claude_client import MODEL_HAIKU, get_client
from timeblock_agent.config import Rules
from timeblock_agent.eventkit_bridge import CalendarEvent

logger = logging.getLogger("timeblock_agent.classify")

Classification = str  # "FIXED" | "FLEXIBLE_BUCKET" | "FLEXIBLE_SPECIFIC"

_VALID_CLASSIFICATIONS = {"FIXED", "FLEXIBLE_BUCKET", "FLEXIBLE_SPECIFIC"}

_CacheKey = tuple  # (event_id, title, start, end, calendar_title, has_attendees, location, bucket_context_hash)
_classification_cache: dict[_CacheKey, Classification] = {}


def clear_classification_cache() -> None:
    """Called at day-start for a clean slate — also keeps the cache from growing
    unbounded across a long-running (weeks between restarts) LaunchAgent process."""
    _classification_cache.clear()


def _bucket_context_hash(bucket_context: str) -> str:
    return hashlib.sha256(bucket_context.encode()).hexdigest()[:16]


def _cache_key(ev: CalendarEvent, bucket_context_hash: str) -> _CacheKey:
    return (
        ev.identifier, ev.title, ev.start, ev.end, ev.calendar_title, ev.has_attendees, ev.location,
        bucket_context_hash,
    )

_SYSTEM_TEMPLATE = """\
You classify calendar events for a time-blocking agent. Every event gets exactly one tag:

- FIXED: meetings, classes, anything with other attendees, an explicit location, or clear \
commitment language in the title (e.g. "call with", "dentist", "flight to"). Real \
commitments the agent must never move, edit, or delete.

- FLEXIBLE_BUCKET: a generic, open-ended personal block with no specific activity named — \
a container the agent should fill with a specific task pulled from Reminders or a \
long-term goal (e.g. a block titled "Work" with nothing more specific said).

- FLEXIBLE_SPECIFIC: a personal block that already names a concrete, self-contained \
activity (e.g. "Gym", "Lunch", "Shower", "Wake up + brush + shower") — nothing to fill \
in. Still fair game to move or resize during a reshuffle, just never replace its content.

Default FIXED over FLEXIBLE when genuinely ambiguous — protecting a real commitment by \
mistake is a minor annoyance, auto-moving one is a broken trust problem. Default \
FLEXIBLE_SPECIFIC over FLEXIBLE_BUCKET when genuinely ambiguous between the two — safer \
to leave a block's content alone than to overwrite something that was already specific.

You will never be shown an event whose title matches an always-FIXED hint — those are
already handled before you see anything.

The user's own description of what counts as a bucket for them, including their current
buckets by name and spirit (not just exact title matches — a renamed or new block in the
same spirit still counts). Trust this over guessing from title-shape alone when it says
something specific:

{bucket_context}

Classify every event given to you. Always call the classify_events tool with one entry per event.
"""

_CLASSIFY_TOOL = {
    "name": "classify_events",
    "description": "Classify each given calendar event as FIXED, FLEXIBLE_BUCKET, or FLEXIBLE_SPECIFIC.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "classification": {
                            "type": "string",
                            "enum": ["FIXED", "FLEXIBLE_BUCKET", "FLEXIBLE_SPECIFIC"],
                        },
                    },
                    "required": ["event_id", "classification"],
                },
            }
        },
        "required": ["classifications"],
    },
}


@dataclass
class ClassifiedEvent:
    event: CalendarEvent
    classification: Classification


def _build_system_prompt(bucket_context: str) -> str:
    return _SYSTEM_TEMPLATE.format(
        bucket_context=bucket_context or "(none configured yet — use judgment from title/context alone)",
    )


def _event_to_prompt_dict(ev: CalendarEvent) -> dict:
    return {
        "event_id": ev.identifier,
        "title": ev.title,
        "start": ev.start.isoformat(),
        "end": ev.end.isoformat(),
        "has_attendees": ev.has_attendees,
        "location": ev.location,
        "calendar": ev.calendar_title,
    }


def _matches_always_fixed_hint(title: str, rules: Rules) -> bool:
    lowered = title.lower()
    return any(hint.lower() in lowered for hint in rules.always_fixed_hints)


def classify_events(events: list[CalendarEvent], rules: Rules, bucket_context: str = "") -> dict[str, Classification]:
    """Returns {event_id: classification}. Raises on API failure — caller must no-op on error.

    A title matching always_fixed_hints is forced FIXED in code, before the event ever
    reaches the model — not just weighted in the prompt. Live testing showed a bare
    "Meeting" title (no attendees, no location — nothing else reinforcing the hint) get
    classified FLEXIBLE_BUCKET and filled with a task despite "meeting" being an explicit
    hint, which is exactly the kind of misclassification the spec's "ambiguous defaults to
    FIXED" philosophy exists to prevent. Getting FIXED wrong is a broken-trust problem;
    getting FLEXIBLE_BUCKET wrong over a hint match is not worth the risk to save a call.

    Anything already classified this same day, with none of its own fields changed and
    against the same bucket_context, is served from cache — a tick where nothing changed
    skips the Claude call entirely (see module docstring, "Pre-call diff check").
    """
    if not events:
        return {}

    ctx_hash = _bucket_context_hash(bucket_context)
    result: dict[str, Classification] = {}
    remaining = []
    cache_hits = 0
    for e in events:
        if _matches_always_fixed_hint(e.title, rules):
            result[e.identifier] = "FIXED"
            continue
        cached = _classification_cache.get(_cache_key(e, ctx_hash))
        if cached is not None:
            result[e.identifier] = cached
            cache_hits += 1
        else:
            remaining.append(e)

    if not remaining:
        if cache_hits:
            logger.info("classify_events: %d event(s) fully served from cache — API call skipped", cache_hits)
        return result
    if cache_hits:
        logger.info(
            "classify_events: %d event(s) served from cache, calling API for the other %d",
            cache_hits, len(remaining),
        )

    client = get_client()
    system_prompt = _build_system_prompt(bucket_context)
    user_content = json.dumps([_event_to_prompt_dict(e) for e in remaining], indent=2)

    response = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=4096,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_events"},
        messages=[{"role": "user", "content": user_content}],
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    for item in tool_use.input["classifications"]:
        classification = item["classification"]
        if classification in _VALID_CLASSIFICATIONS:
            result[item["event_id"]] = classification

    # Safety net: anything the model didn't return a valid answer for defaults to FIXED.
    for e in remaining:
        result.setdefault(e.identifier, "FIXED")

    for e in remaining:
        _classification_cache[_cache_key(e, ctx_hash)] = result[e.identifier]

    return result
