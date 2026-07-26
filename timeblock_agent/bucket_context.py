"""Loads the user-editable bucket-context file (see buckets.md) — freeform prose
describing what generally counts as a "bucket" (FLEXIBLE_BUCKET) vs. an already-specific
block (FLEXIBLE_SPECIFIC), plus the user's actual current buckets by name and spirit/
priority. Read fresh every cycle and fed as context to both classification and
incremental-replan priority judgments.

Deliberately not a keyword/hint list: bucket names change over time (e.g. once school
starts, "Work" may split into "Project Time"/"Classwork"/"Revision"), and a model reading
a description generalizes to a renamed bucket in a way exact-string matching can't.
"""

from __future__ import annotations

import os
from pathlib import Path

# TIMEBLOCK_BUCKETS_PATH lets a sandboxed test run point at its own bucket descriptions
# (buckets.test.md) instead of the real ones — see TESTING.md.
DEFAULT_BUCKETS_PATH = Path(
    os.environ.get("TIMEBLOCK_BUCKETS_PATH", str(Path(__file__).resolve().parent.parent / "buckets.md"))
)


def load_bucket_context(path: Path = DEFAULT_BUCKETS_PATH) -> str:
    """Returns the file's raw content, or "" if it doesn't exist yet. Callers should
    treat empty as "nothing configured" and fall back to judgment from titles alone."""
    if not path.exists():
        return ""
    return path.read_text().strip()
