"""Turns a week's worth of completion-log patterns + detected manual calendar edits into
a few sentences of plain-language observations, appended to preferences.md (see
preferences.py, rules.md, and orchestrator.run_weekly_review — the caller that gathers
the inputs this module turns into prose).

Unlike every other Claude call in this codebase, this one produces a *descriptive*
summary, not a consequential scheduling decision — nothing downstream trusts its output
unchecked the way validate_block/validate_bucket_adjustment gate the day-to-day calls.
That's exactly why a model call is the right tool here specifically: distilling a pile of
stats into a few readable sentences is squarely what it's good at, without the
"can't-fully-trust-prompt-following" risk that applies to placing real calendar events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from timeblock_agent.claude_client import MODEL_HAIKU, get_client

_SYSTEM_PROMPT = """\
You write short, plain-language observations about how someone's time-blocking schedule \
went this past week, for them to read later as a quick reminder of what's been trending. \
You're given per-bucket completion-log stats (how often tasks in each bucket finished on \
time vs. ran over vs. got displaced) and any agent-authored calendar blocks the person \
edited by hand this week (retitled, resized, or deleted outside the normal check-in flow).

Write 2-5 short sentences, in second person ("you"), noting only what's genuinely worth \
remembering — a bucket that consistently runs long, a task type that keeps getting \
bumped, a manual edit pattern that suggests the agent's estimates or judgment should lean \
differently for something specific. Skip anything that's just normal variation. Don't \
restate the raw numbers back verbatim — synthesize what they suggest. If nothing in the \
data is actually notable, say so plainly in one short sentence rather than manufacturing \
an observation.

Always call the write_observations tool exactly once with your result.
"""

_TOOL = {
    "name": "write_observations",
    "description": "Report this week's observations as plain text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observations": {
                "type": "string",
                "description": "2-5 short sentences, plain language, second person.",
            }
        },
        "required": ["observations"],
    },
}


@dataclass
class BucketStats:
    bucket: str
    completed: int
    bumped: int
    still_in_progress: int


@dataclass
class ManualEdit:
    kind: str  # "retitled" | "resized" | "deleted"
    snapshot_title: str
    snapshot_start: datetime
    snapshot_end: datetime
    current_title: Optional[str] = None  # None if kind == "deleted"
    current_start: Optional[datetime] = None
    current_end: Optional[datetime] = None


def _build_user_payload(bucket_stats: list[BucketStats], manual_edits: list[ManualEdit]) -> dict:
    return {
        "bucket_stats": [
            {
                "bucket": s.bucket, "completed": s.completed,
                "bumped": s.bumped, "still_in_progress": s.still_in_progress,
            }
            for s in bucket_stats
        ],
        "manual_edits": [
            {
                "kind": e.kind,
                "written_by_agent": {
                    "title": e.snapshot_title,
                    "start": e.snapshot_start.isoformat(),
                    "end": e.snapshot_end.isoformat(),
                },
                "current": (
                    None if e.kind == "deleted" else
                    {"title": e.current_title, "start": e.current_start.isoformat(), "end": e.current_end.isoformat()}
                ),
            }
            for e in manual_edits
        ],
    }


def generate_observations(bucket_stats: list[BucketStats], manual_edits: list[ManualEdit]) -> Optional[str]:
    """Returns None (no Claude call made) if there's nothing at all to analyze — an
    empty week shouldn't burn a call or pad preferences.md with filler. Raises on API
    failure — caller should treat that as "skip this week's observation" rather than
    guess, same no-op-on-failure pattern as every other Claude call here."""
    if not bucket_stats and not manual_edits:
        return None

    client = get_client()
    user_payload = _build_user_payload(bucket_stats, manual_edits)

    response = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=1024,
        system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "write_observations"},
        messages=[{"role": "user", "content": json.dumps(user_payload, indent=2)}],
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input["observations"].strip()
