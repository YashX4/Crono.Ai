"""Diffs proposed day-layout blocks against what's already in the agent calendar and
writes only the deltas (create/update/delete) — never a full teardown-and-rebuild
(see spec: "Diff/write logic: compare proposed layout to current calendar, write only
deltas to FLEXIBLE events").

Correlation between a proposed block and an existing agent-calendar event is tracked via
a machine-readable marker embedded in the event's notes field, since EventKit has no
custom per-event metadata slot and a block's bucket/reminder-or-goal source can't be
recovered from title/time alone across reruns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from timeblock_agent.block_snapshots import record_snapshot, remove_snapshot
from timeblock_agent.day_layout import ProposedBlock
from timeblock_agent.eventkit_bridge import CalendarEvent, EventKitBridge

_META_RE = re.compile(r"<!-- agent-meta: (\{.*?\}) -->")

Key = tuple[str, str, str]  # (bucket_event_id, source, source_id)


def encode_block_meta(block: ProposedBlock) -> str:
    meta = {"bucket_event_id": block.bucket_event_id, "source": block.source, "source_id": block.source_id}
    return f"<!-- agent-meta: {json.dumps(meta)} -->"


def decode_block_meta(notes) -> dict | None:
    if not notes:
        return None
    m = _META_RE.search(notes)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _key_from_meta(meta: dict) -> Key:
    return (meta["bucket_event_id"], meta["source"], meta["source_id"])


def _key_from_block(block: ProposedBlock) -> Key:
    return (block.bucket_event_id, block.source, block.source_id)


@dataclass
class WriteSummary:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def apply_layout(
    bridge: EventKitBridge,
    proposed: list[ProposedBlock],
    existing_agent_events: list[CalendarEvent],
    agent_calendar: str,
    written_at: Optional[datetime] = None,
) -> WriteSummary:
    """existing_agent_events should be every event currently in `agent_calendar` for the
    window under consideration (e.g. the rest of today). Anything among them that isn't
    agent-authored (no parseable marker) is left untouched and ignored.

    `written_at` is optional and threads through to record_snapshot's own `written_at`
    (defaults to real time if omitted) — lets a caller running against a synthetic/fake
    clock (see fake_sandbox/) keep snapshot timestamps consistent with that same clock,
    rather than silently falling back to real wall-clock time regardless."""
    existing_by_key: dict[Key, CalendarEvent] = {}
    for ev in existing_agent_events:
        meta = decode_block_meta(ev.notes)
        if meta is not None:
            existing_by_key[_key_from_meta(meta)] = ev

    summary = WriteSummary()
    proposed_keys: set[Key] = set()

    for block in proposed:
        key = _key_from_block(block)
        proposed_keys.add(key)
        notes = encode_block_meta(block)
        existing = existing_by_key.get(key)

        if existing is None:
            identifier = bridge.create_event(
                title=block.title,
                start=block.start,
                end=block.end,
                calendar_title=agent_calendar,
                notes=notes,
            )
            summary.created.append(identifier)
        elif existing.title != block.title or existing.start != block.start or existing.end != block.end:
            bridge.update_event(
                existing.identifier, title=block.title, start=block.start, end=block.end, notes=notes
            )
            summary.updated.append(existing.identifier)
            identifier = existing.identifier
        else:
            summary.unchanged.append(existing.identifier)
            identifier = existing.identifier

        # Every create/update/confirmed-unchanged refreshes the snapshot the weekly
        # review later diffs against — it should always reflect the system's own most
        # recent write, so only a divergence it didn't cause looks like a manual edit.
        record_snapshot(
            identifier, block.title, block.start, block.end, block.bucket_event_id, block.source, block.source_id,
            written_at=written_at,
        )

    for key, ev in existing_by_key.items():
        if key not in proposed_keys:
            bridge.delete_event(ev.identifier)
            summary.deleted.append(ev.identifier)
            remove_snapshot(ev.identifier)

    return summary
