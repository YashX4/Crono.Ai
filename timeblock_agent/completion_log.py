"""Daily completion log writer (see spec: "Completion logging").

Every task block gets logged, not just goal-fills, mirroring goals.py's append-only log
pattern — one file at ~/timeblock-logs/YYYY-MM-DD.md per day instead of per project.
Called whenever a block actually resolves (its end time passes, an overrun gets
answered, or a day boundary finalizes the day) — not at proposal time, since a block's
status isn't known until it resolves.

Each entry is written as one JSON object per line (a "- " bullet prefix kept only so the
file still reads as a markdown list), e.g.:

    - {"start": "09:00", "end": "10:00", "task": "...", "bucket": "...", "status": "completed", "source": "reminder"}

Earlier versions of this module used a hand-rolled
"- {start}-{end} {task} ({bucket}) — {status} [{source}]" format parsed with a regex.
That format was genuinely ambiguous whenever `task` or `bucket` contained their own
literal parentheses (e.g. task "Test task beta (~20 min)" combined with bucket "Task
Blocks (Test)") — no greedy/non-greedy regex choice could split both shapes correctly at
once (see ISSUES.md). JSON-lines sidesteps that entirely: `json.dumps`/`json.loads`
escape whatever the fields contain, so the split point is never ambiguous regardless of
what characters `task`/`bucket` hold. This is a breaking format change — no read-
compatibility with old on-disk log files is provided or needed (pre-ship, sandbox/dev
data only, see ISSUES.md).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# TIMEBLOCK_LOG_DIR lets a sandboxed test run keep its own completion log, separate from
# real history — see TESTING.md.
DEFAULT_LOG_DIR = Path(os.environ.get("TIMEBLOCK_LOG_DIR", str(Path.home() / "timeblock-logs")))

STATUSES = ("completed", "bumped", "still-in-progress")
SOURCES = ("reminder", "goal-fill", "external")  # "external" = a FIXED/FLEXIBLE_SPECIFIC
# block (meeting, gym, etc.) the agent never authored or resizes, just checks in on


def _log_path(for_date: date, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{for_date:%Y-%m-%d}.md"


def parse_log_line(line: str, for_date: date) -> Optional[dict]:
    """Parses one line back into its structured fields — the inverse of log_block()'s
    own formatting, kept in this same module so the two can't drift apart. Returns None
    for anything that isn't a well-formed entry line (e.g. the "# YYYY-MM-DD" day header,
    a blank line, or a malformed/corrupt line) rather than raising."""
    line = line.strip()
    if not line.startswith("- "):
        return None
    try:
        obj = json.loads(line[2:])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        start_str, end_str = obj["start"], obj["end"]
        task, bucket = obj["task"], obj["bucket"]
        status, source = obj["status"], obj["source"]
    except (KeyError, TypeError):
        return None
    if status not in STATUSES or source not in SOURCES:
        return None
    try:
        start = datetime.combine(for_date, datetime.strptime(start_str, "%H:%M").time())
        end = datetime.combine(for_date, datetime.strptime(end_str, "%H:%M").time())
    except (ValueError, TypeError):
        return None
    return {"start": start, "end": end, "task": task, "bucket": bucket, "status": status, "source": source}


def read_logs_between(since: date, until: date, log_dir: Path = DEFAULT_LOG_DIR) -> list[dict]:
    """Returns every parsed log entry across [since, until] inclusive, skipping any day
    with no log file (a day nothing resolved, or the agent wasn't running) — used by the
    weekly review to aggregate patterns (see weekly_review.py)."""
    entries: list[dict] = []
    current = since
    while current <= until:
        path = _log_path(current, log_dir)
        if path.exists():
            for line in path.read_text().splitlines():
                parsed = parse_log_line(line, current)
                if parsed is not None:
                    entries.append(parsed)
        current += timedelta(days=1)
    return entries


def log_block(
    start: datetime,
    end: datetime,
    task: str,
    bucket: str,
    status: str,
    source: str,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> None:
    """Appends one line for a resolved task block to the log file for `start`'s date."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")

    path = _log_path(start.date(), log_dir)
    is_new = not path.exists()
    entry = {
        "start": f"{start:%H:%M}",
        "end": f"{end:%H:%M}",
        "task": task,
        "bucket": bucket,
        "status": status,
        "source": source,
    }
    line = f"- {json.dumps(entry, ensure_ascii=False)}\n"

    with open(path, "a") as f:
        if is_new:
            f.write(f"# {start:%Y-%m-%d}\n\n")
        f.write(line)
