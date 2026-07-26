"""Manual visual check for the settings page — renders it without a running server,
a webhook token, or the menu bar app. Also round-trips a fake submission through
parse_form_to_rules_dict -> config._rules_from_dict to confirm nothing broke.

Run with: .venv/bin/python scripts/preview_settings_page.py
"""

import sys
import tempfile
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.config import _rules_from_dict
from timeblock_agent.settings_page import parse_form_to_rules_dict, render_settings_form

# Mirrors the live rules.yaml defaults/comments so the preview looks like a real install.
FAKE_RAW = {
    "morning_checkin_floor": "09:00",
    "evening_checkin_floor": "22:00",
    "day_boundary_gap_hours": 4,
    "floor_confirmation_retry_minutes": 30,
    "safety_backstop_minutes": 60,
    "followup_delay_unexpected_minutes": 30,
    "followup_delay_continuing_minutes": 60,
    "min_block_minutes": 15,
    "buffer_block_minutes": 15,
    "agent_calendar": "Task Blocks",
    "calendars": {"include": [], "exclude": ["Classes", "Family", "US Holidays", "Birthdays"]},
    "reminder_lists": {"include": [], "exclude": []},
    "protected_buffers": [
        {"match": ["buffer", "wind down", "wind-down"], "allow_goal_fill": True},
        {"match": ["goal time"], "allow_goal_fill": True},
    ],
    "always_fixed_hints": ["class", "lecture", "meeting", "call", "appointment"],
    "goal_fill": {"weight_staleness": 0.6, "weight_priority": 0.4, "max_minutes_per_goal": 180},
    "weekly_review_interval_days": 7,
    "weekly_review_retry_minutes": 60,
}


class _FakeForm(dict):
    """Minimal stand-in for Starlette's FormData — parse_form_to_rules_dict only ever
    calls .get(key, default) on it."""


def _write_and_open(name: str, html: str, out_dir: Path) -> None:
    path = out_dir / name
    path.write_text(html)
    print(f"  {name}: file://{path}")
    webbrowser.open(f"file://{path}")


def main():
    out_dir = Path(tempfile.mkdtemp(prefix="crono_settings_preview_"))
    print(f"Writing previews to {out_dir}")

    _write_and_open("default.html", render_settings_form(FAKE_RAW, token="preview-token"), out_dir)
    _write_and_open(
        "error.html",
        render_settings_form(FAKE_RAW, token="preview-token", error="agent_calendar must not be blank"),
        out_dir,
    )
    _write_and_open(
        "saved.html",
        render_settings_form(FAKE_RAW, token="preview-token", saved=True),
        out_dir,
    )
    _write_and_open("fresh_install.html", render_settings_form({}, token="preview-token"), out_dir)

    # --- Round-trip check: submitted form -> parse_form_to_rules_dict -> _rules_from_dict ---
    fake_form = _FakeForm({
        "token": "preview-token",
        "morning_checkin_floor": "09:00",
        "evening_checkin_floor": "22:00",
        "day_boundary_gap_hours": "4",
        "floor_confirmation_retry_minutes": "30",
        "safety_backstop_minutes": "60",
        "followup_delay_unexpected_minutes": "30",
        "followup_delay_continuing_minutes": "60",
        "min_block_minutes": "15",
        "buffer_block_minutes": "15",
        "agent_calendar": "Task Blocks",
        "calendars_include": "",
        "calendars_exclude": "Classes\nFamily\nUS Holidays\nBirthdays",
        "reminder_lists_include": "",
        "reminder_lists_exclude": "",
        "always_fixed_hints": "class\nlecture\nmeeting\ncall\nappointment",
        "goal_fill_weight_staleness": "0.6",
        "goal_fill_weight_priority": "0.4",
        "goal_fill_max_minutes_per_goal": "180",
        "protected_buffer_match_0": "buffer\nwind down\nwind-down",
        "protected_buffer_allow_goal_fill_0": "on",
        "protected_buffer_match_1": "goal time",
        "protected_buffer_allow_goal_fill_1": "on",
        "protected_buffer_match_2": "",
        "protected_buffer_match_3": "",
    })

    parsed = parse_form_to_rules_dict(fake_form)
    rules = _rules_from_dict(parsed)
    print("\nRound-trip OK:")
    print(f"  agent_calendar = {rules.agent_calendar!r}")
    print(f"  calendars_exclude = {rules.calendars_exclude!r}")
    print(f"  protected_buffers = {rules.protected_buffers!r}")
    print(f"  goal_fill_weights = {rules.goal_fill_weights!r}")


if __name__ == "__main__":
    main()
