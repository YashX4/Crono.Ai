"""Conversational reminder intake via Telegram (see rules.md §10, timeblock-agent-spec.md
"Planned but not yet built"). You text the bot a reminder in your own words; one call here
decides whether the task itself is clear enough to act on (extracting a title/notes/optional
due date) or asks a single clarifying question back.

This is the first genuinely multi-turn call in this codebase — every other prompt module
(classify.py, day_layout.py, incremental_replan.py, weekly_review.py) is single-shot. The
caller (orchestrator.handle_reminder_message) is responsible for carrying the transcript
across separate Telegram messages via AgentState.pending_reminder_intake; this module just
takes whatever `messages` it's given and resolves one more turn.

Deliberately runs against a LOCAL OLLAMA MODEL, not Claude — the one scoped exception to
this codebase's otherwise Anthropic-only stance (see claude_client.py), because every
incoming Telegram message is a new call and this could fire far more often per day than the
existing once-or-twice-daily calls. Uses Ollama's `format` JSON-schema parameter
(grammar-constrained decoding — an invalid JSON *shape* is mechanically impossible under
this mode), not Ollama's separate tool-calling API, since a single discriminated-union
response is exactly what a single API call needs here. Requires `ollama serve` (or the
packaged app) running locally with OLLAMA_MODEL pulled — see BUILD_LOG.md's setup notes.

API/connection failure raises OllamaError — callers must catch and treat as "try again
later" (same "never crash the scheduler over a model hiccup" posture as every other call
site in this codebase), not silently guess at an answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("timeblock_agent.reminder_intake")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")

_VALID_ACTIONS = {"ready", "clarify", "cancel"}
_DEFAULT_DUE_HOUR = 9  # a due date with no time stated (e.g. "Friday") defaults to 09:00
_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class OllamaError(RuntimeError):
    pass


@dataclass
class ReminderIntakeResult:
    action: str  # "ready" | "clarify" | "cancel"
    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    clarifying_question: Optional[str] = None
    # False only when a prior draft existed AND the latest message is about something
    # else entirely — every other field above is then resolved from the latest message
    # ALONE, ignoring the earlier turns (see resolve()'s docstring).
    same_topic: bool = True


_SYSTEM_TEMPLATE = """\
You turn a plain-language Telegram message into a Reminders.app reminder for a \
time-blocking assistant. Today is {weekday}, {today}.

Decide exactly one of three actions:

- "ready": the task itself names a concrete action clearly enough to act on. Extract:
  - title: rephrased as an imperative, e.g. "Call the dentist" not "I need to call the \
dentist" or "call the dentist".
  - notes: any extra detail the user gave that isn't part of the title itself (optional \
— omit if there's nothing beyond the title).
  - due_date: ONLY if the user actually stated one, resolved against today's real date \
above (e.g. "Friday" -> the coming Friday's date). Omit entirely if no due date was \
mentioned — that's the normal case for most reminders, never something to ask about. If \
the user only stated a date with no specific time, return just the date (YYYY-MM-DD) — \
never invent a time of day that wasn't actually said.

- "clarify": the task itself is too vague to act on — no concrete action named at all \
(e.g. "remind me to do that thing", "remind me about the thing with mom"). Ask exactly \
ONE short, specific plain-text question that would resolve it. Never ask a clarifying \
question just because a due date wasn't mentioned — only when the task's content itself \
is unclear.

- "cancel": the user is calling off this reminder in plain language (e.g. "never mind", \
"forget it", "cancel that", "nvm").

If this is a continuing conversation (earlier turns already asked a clarifying question), \
use the user's latest reply together with that earlier context to resolve to "ready" or \
"cancel" if now possible, or ask a further single clarifying question only if genuinely \
still unclear.

If a draft was already proposed (an earlier "Proposed: ..." turn) and the user's latest \
message pushes back, points out a problem, or asks for something different about THAT \
SAME task — rather than a plain confirmation or cancellation — treat it as feedback and \
revise the draft accordingly (a new "ready" with corrected fields). If their pushback \
points out a problem WITHOUT giving you the replacement value (e.g. "who does anything at \
midnight?" after you proposed a midnight due time) — don't guess a new value yourself; \
acknowledge the mistake briefly and ask for the actual value you're missing (e.g. "My bad \
— what time works?"), using "clarify". Only use "cancel" here if they're actually calling \
the whole thing off, not just correcting a detail.

same_topic: if a draft was already proposed, is the user's LATEST message actually about \
that SAME task, or something else entirely (e.g. you proposed a dentist reminder and they \
now ask about booking a restaurant instead)? Set to false for a genuine topic change. When \
false, resolve action/title/notes/due_date/clarifying_question from the LATEST message \
ALONE, as if starting a brand new conversation — completely ignore the earlier turns, \
don't blend the old task's details into the new one. Always true if there's no earlier \
proposed draft yet (nothing to compare against).

Always respond with the resolve_reminder structure — action, plus only the fields that \
action actually uses. When action is "clarify", clarifying_question is REQUIRED — never \
leave it empty.
"""

_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ready", "clarify", "cancel"]},
        "title": {"type": "string"},
        "notes": {"type": "string"},
        "due_date": {"type": "string", "description": "ISO 8601 date or datetime, only if the user stated one"},
        "clarifying_question": {"type": "string"},
        "same_topic": {
            "type": "boolean",
            "description": "false only if a prior draft existed and the latest message is about something else entirely",
        },
    },
    "required": ["action", "same_topic"],
}


def _build_system_prompt(now: datetime) -> str:
    return _SYSTEM_TEMPLATE.format(weekday=now.strftime("%A"), today=now.date().isoformat())


def _parse_due_date(value: Optional[str]) -> Optional[datetime]:
    """A date with no real time stated defaults to 09:00 — bridge.create_reminder always
    sets hour/minute components, there's no all-day path. Applied whenever the parsed
    result lands on exact midnight, regardless of whether the model returned a bare date
    or a full datetime with a zeroed-out time — the prompt asks for a bare date when no
    time was stated, but a small local model doesn't reliably follow that, so the default
    is enforced here rather than trusted from prompt-following alone (same "code-level
    check, not just prompt wording" pattern used throughout this codebase). Anything
    unparseable is treated as "no due date" (safer than blocking the whole reminder over a
    malformed date)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            d = date.fromisoformat(value)
            dt = datetime(d.year, d.month, d.day)
        except ValueError:
            logger.warning("reminder_intake: unparseable due_date %r from model — ignoring", value)
            return None
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt.replace(hour=_DEFAULT_DUE_HOUR)
    return dt


def _deterministic_relative_date(latest_user_text: str, now: datetime) -> Optional[date]:
    """Overrides the model's OWN date arithmetic for the small set of bare, unambiguous
    relative-date words it's demonstrably unreliable at computing correctly itself —
    caught live: qwen2.5:14b resolved "monday" (said on a Sunday) to the following
    Tuesday, consistently, not a one-off. Same "code-level check, not prompt wording
    alone" pattern this codebase uses everywhere else for anything consequential — a wrong
    due date on a reminder is a real, not cosmetic, failure. A bare weekday name resolves
    to its very next occurrence, INCLUDING today (saying "Friday" on a Friday means
    today, not a week from now). Only covers today/tomorrow/a bare weekday name; anything
    else ("next Tuesday", "in 3 days", a specific calendar date) is left to the model."""
    words = set(re.findall(r"[a-z]+", latest_user_text.lower()))
    if "today" in words:
        return now.date()
    if "tomorrow" in words:
        return now.date() + timedelta(days=1)
    for name, target_weekday in _WEEKDAY_NAMES.items():
        if name in words:
            delta = (target_weekday - now.weekday()) % 7
            return now.date() + timedelta(days=delta)
    return None


def _latest_user_message(messages: list[dict]) -> str:
    return next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")


def resolve(messages: list[dict], now: datetime) -> ReminderIntakeResult:
    """messages: the full stored transcript so far, each a plain {"role", "content"} turn
    (the caller's newest user message already appended). Raises OllamaError on any
    connection/API failure — the caller is responsible for preserving the pending thread
    and telling the user to try again rather than losing the conversation."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": _build_system_prompt(now)}] + messages,
        "format": _RESULT_SCHEMA,
        "stream": False,
    }

    try:
        response = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=120)
    except httpx.HTTPError as e:
        raise OllamaError(f"Could not reach Ollama at {OLLAMA_BASE_URL}: {e}") from e

    if response.status_code >= 300:
        raise OllamaError(f"Ollama API error {response.status_code}: {response.text}")

    try:
        raw = json.loads(response.json()["message"]["content"])
    except (KeyError, json.JSONDecodeError) as e:
        raise OllamaError(f"Malformed response from Ollama: {e}") from e

    action = raw.get("action")
    if action not in _VALID_ACTIONS:
        # Safety net, same spirit as classify.py's setdefault("FIXED") — content quality
        # isn't guaranteed just because the JSON shape is (grammar-constrained decoding
        # only enforces shape, not that the model picked a valid enum value in practice).
        logger.warning("reminder_intake: invalid/missing action %r from model — defaulting to clarify", action)
        return ReminderIntakeResult(
            action="clarify",
            clarifying_question="Sorry, could you say that again — what would you like to be reminded of?",
        )

    clarifying_question = raw.get("clarifying_question")
    if action == "clarify" and not clarifying_question:
        # Grammar-constrained decoding guarantees the JSON *shape* but not that the model
        # actually filled in a question it was supposed to — caught live: a real reply of
        # action="clarify" with clarifying_question=null sent a blank "Quick question" with
        # nothing after it. Same defense-in-depth spirit as the invalid-action fallback above.
        logger.warning("reminder_intake: action=clarify but no clarifying_question — using a generic one")
        clarifying_question = "Sorry, could you say a bit more about what you'd like to be reminded of?"

    due_date = _parse_due_date(raw.get("due_date"))
    if due_date is not None:
        override_date = _deterministic_relative_date(_latest_user_message(messages), now)
        if override_date is not None and override_date != due_date.date():
            logger.info(
                "reminder_intake: correcting model's due_date %s -> %s (deterministic weekday/today/tomorrow match)",
                due_date.date(), override_date,
            )
            due_date = datetime.combine(override_date, due_date.time())

    same_topic = raw.get("same_topic")
    if not isinstance(same_topic, bool):
        same_topic = True  # safer default: treat as a continuation, never silently orphan real feedback

    return ReminderIntakeResult(
        action=action,
        title=raw.get("title"),
        notes=raw.get("notes"),
        due_date=due_date,
        clarifying_question=clarifying_question,
        same_topic=same_topic,
    )
