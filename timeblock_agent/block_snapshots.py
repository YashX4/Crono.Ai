"""Tracks "as the agent most recently wrote it" for every agent-authored task block, so
the weekly review (see weekly_review.py) can notice when you've silently corrected one by
hand — retitled, resized, or deleted it directly in Calendar.app, outside the normal
check-in flow.

Every agent-authored write already funnels through diff_write.apply_layout(); that's the
only place this module's record/remove functions get called. A snapshot always holds the
system's OWN last write, so when apply_layout itself later updates or deletes a block (a
normal reshuffle, not a manual edit), the snapshot is refreshed or removed at the same
moment — only a divergence the system didn't cause survives until review time.

Scope: agent-authored task blocks only (the dedicated agent_calendar), not bucket
container resizes — see rules.md for why that's a deliberate boundary, not an oversight.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# TIMEBLOCK_BLOCK_SNAPSHOTS_PATH lets a sandboxed test run keep its own snapshot store,
# separate from real tracked blocks — see TESTING.md.
DEFAULT_SNAPSHOTS_PATH = Path(
    os.environ.get("TIMEBLOCK_BLOCK_SNAPSHOTS_PATH", str(Path.home() / ".timeblock-agent" / "block_snapshots.json"))
)


@dataclass
class BlockSnapshot:
    identifier: str
    title: str
    start: datetime
    end: datetime
    bucket_event_id: str
    source: str
    source_id: str
    written_at: datetime

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["written_at"] = self.written_at.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "BlockSnapshot":
        return cls(
            identifier=d["identifier"],
            title=d["title"],
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            bucket_event_id=d["bucket_event_id"],
            source=d["source"],
            source_id=d["source_id"],
            written_at=datetime.fromisoformat(d["written_at"]),
        )


def load_snapshots(path: Path = DEFAULT_SNAPSHOTS_PATH) -> dict[str, BlockSnapshot]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {identifier: BlockSnapshot.from_json(d) for identifier, d in raw.items()}


def save_snapshots(snapshots: dict[str, BlockSnapshot], path: Path = DEFAULT_SNAPSHOTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({identifier: s.to_json() for identifier, s in snapshots.items()}, indent=2))


def record_snapshot(
    identifier: str,
    title: str,
    start: datetime,
    end: datetime,
    bucket_event_id: str,
    source: str,
    source_id: str,
    written_at: Optional[datetime] = None,
    path: Path = DEFAULT_SNAPSHOTS_PATH,
) -> None:
    """Called from diff_write.apply_layout() on every create/update — always overwrites
    any existing entry for this identifier, so the snapshot only ever reflects the
    system's most recent own write."""
    snapshots = load_snapshots(path)
    snapshots[identifier] = BlockSnapshot(
        identifier=identifier, title=title, start=start, end=end,
        bucket_event_id=bucket_event_id, source=source, source_id=source_id,
        written_at=written_at or datetime.now(),
    )
    save_snapshots(snapshots, path)


def remove_snapshot(identifier: str, path: Path = DEFAULT_SNAPSHOTS_PATH) -> None:
    """Called from diff_write.apply_layout() whenever IT deletes a block — removing the
    snapshot at the same moment is what keeps a routine system-initiated deletion from
    ever looking like a manual one at review time."""
    snapshots = load_snapshots(path)
    if identifier in snapshots:
        del snapshots[identifier]
        save_snapshots(snapshots, path)
