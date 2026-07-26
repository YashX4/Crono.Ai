"""Loads/updates `preferences.md` — the weekly-review learning loop's output (see
weekly_review.py, rules.md). Lives at the repo root and is read fresh every cycle, same
as buckets.md, since it's the same kind of thing: freeform steering text you can edit
directly.

Mirrors goals.py's frontmatter+notes / append-only-log split: a manual section you edit
yourself, and an "## Observations" section the weekly review only ever appends to, never
rewrites — a regeneration can never clobber something you wrote yourself.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# TIMEBLOCK_PREFERENCES_PATH lets a sandboxed test run use its own preferences file —
# see TESTING.md.
DEFAULT_PREFERENCES_PATH = Path(
    os.environ.get("TIMEBLOCK_PREFERENCES_PATH", str(Path(__file__).resolve().parent.parent / "preferences.md"))
)

OBSERVATIONS_HEADER = "## Observations (agent-appended weekly — do not hand-edit above this line)"
OBSERVATIONS_MARKER = "<!-- Agent appends one line per weekly review below. Do not hand-edit above this line. -->"

_TEMPLATE = f"""\
## My preferences (edit this yourself — never auto-changed)

(nothing here yet — write in anything you want the agent to weigh, in your own words)

{OBSERVATIONS_HEADER}
{OBSERVATIONS_MARKER}
"""


def _ensure_file(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE)


def load_preferences(max_observations: int = 8, path: Path = DEFAULT_PREFERENCES_PATH) -> str:
    """Returns the manual section in full, plus only the last `max_observations` entries
    — the file grows forever, daily prompts shouldn't (same cost-guardrail reasoning
    `bucket_context`/goal content already gets trimmed under). Returns "" if the file
    doesn't exist yet, same as bucket_context.load_bucket_context()."""
    if not path.exists():
        return ""
    text = path.read_text()
    if OBSERVATIONS_MARKER not in text:
        return text.strip()

    manual_section, _, observations_block = text.partition(OBSERVATIONS_MARKER)
    entries = [line for line in observations_block.splitlines() if line.strip().startswith("- ")]
    # Newest-first in the file (each append inserts right after the marker, same
    # convention as goals.py's own log) — so "the last N" means the first N here.
    trimmed = entries[:max_observations] if max_observations else entries

    parts = [manual_section.strip()]
    if trimmed:
        parts.append("## Recent observations\n" + "\n".join(trimmed))
    return "\n\n".join(parts).strip()


def append_observation(entry: str, when: Optional[datetime] = None, path: Path = DEFAULT_PREFERENCES_PATH) -> None:
    """Appends one dated observation line under the marker — never touches anything
    above it, so your own manual section is never rewritten."""
    _ensure_file(path)
    when = when or datetime.now()
    text = path.read_text()
    log_line = f"- {when:%Y-%m-%d}: {entry}"

    if OBSERVATIONS_MARKER in text:
        text = text.replace(OBSERVATIONS_MARKER, f"{OBSERVATIONS_MARKER}\n{log_line}", 1)
    else:
        text = text.rstrip("\n") + f"\n\n{OBSERVATIONS_HEADER}\n{OBSERVATIONS_MARKER}\n{log_line}\n"

    path.write_text(text)
