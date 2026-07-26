"""Renders + parses the local web settings page (see ROADMAP.md's menu bar work,
`server.py`'s GET/POST /settings). Pure rendering/parsing only — no FastAPI, no
filesystem access, so it's trivially testable and keeps server.py from bloating.

Field-for-field mirror of config.Rules — see that module's docstring for what each field
actually does; this one only concerns itself with the HTML <-> nested-dict shape.
"""

from __future__ import annotations

import html
from typing import Optional

# Existing protected_buffers get pre-filled, plus this many blank slots for adding new
# ones — clearing a block's own match text on save is how an existing one gets removed.
_EXTRA_PROTECTED_BUFFER_SLOTS = 2

_LIST_FIELDS = (
    ("calendars_include", "Calendars — include", "One calendar name per line. Empty = include everything not excluded."),
    ("calendars_exclude", "Calendars — exclude", "One calendar name per line. Exclude always wins over include."),
    ("reminder_lists_include", "Reminder lists — include", "One list name per line."),
    ("reminder_lists_exclude", "Reminder lists — exclude", "One list name per line."),
    ("always_fixed_hints", "Always-FIXED hints", "One title keyword per line (e.g. \"meeting\") — forces FIXED before the model ever sees a matching event."),
)

_NUMBER_FIELDS = (
    ("day_boundary_gap_hours", "Day boundary gap (hours)", 4, "any"),
    ("floor_confirmation_retry_minutes", "Floor confirmation retry (minutes)", 30, "1"),
    ("safety_backstop_minutes", "Safety backstop (minutes)", 60, "1"),
    ("followup_delay_unexpected_minutes", "Follow-up delay — unexpected (minutes)", 30, "1"),
    ("followup_delay_continuing_minutes", "Follow-up delay — continuing (minutes)", 60, "1"),
    ("min_block_minutes", "Minimum block length (minutes)", 15, "1"),
    ("buffer_block_minutes", "Buffer between blocks (minutes)", 15, "1"),
    ("weekly_review_interval_days", "Weekly review interval (days)", 7, "any"),
    ("weekly_review_retry_minutes", "Weekly review retry (minutes)", 60, "1"),
    ("reminder_intake_timeout_minutes", "Reminder intake timeout (minutes)", 30, "1"),
)

_NUMBER_FIELDS_BY_NAME = {name: (label, default, step) for name, label, default, step in _NUMBER_FIELDS}

# Plain-English, companion-voice copy for the per-field "?" popovers. Keyed by the same
# `name=` string the form field already uses — not a separate slug — so there's exactly
# one place per field to keep this in sync, and no duplicate lookup keys to maintain.
_HELP: dict[str, str] = {
    "morning_checkin_floor": "The earliest time I'll ask if you're up and ready to start the day — I won't ping you before this, even on a quiet morning.",
    "evening_checkin_floor": "The earliest time I'll ask if you're calling it a day. Set it to whenever your evenings actually tend to wind down.",
    "day_boundary_gap_hours": "If I go silent for this many hours straight — say your Mac was asleep — I'll treat it as a new day once I'm back, instead of getting confused about what day it is.",
    "floor_confirmation_retry_minutes": "If you don't answer my morning or evening check-in, this is how often I'll gently ask again.",
    "followup_delay_unexpected_minutes": "When something unexpected comes up, I'll wait this long before checking back in and adjusting the rest of your day.",
    "followup_delay_continuing_minutes": "When you tell me you're still going on something, I'll wait this long before checking in again to see if you've wrapped up.",
    "safety_backstop_minutes": "This one's just a safety net behind the scenes, making sure I always have something scheduled to wake up for. You won't notice it directly — most people never need to touch it.",
    "min_block_minutes": "I won't schedule a task into anything shorter than this — below a certain size, it's not worth its own slot.",
    "buffer_block_minutes": "How much breathing room I'll try to leave between tasks I schedule back-to-back, so you're not sprinting straight from one into the next.",
    "weekly_review_interval_days": "How often I take a step back, look at how your week actually went, and jot myself a few notes on how to plan you better.",
    "weekly_review_retry_minutes": "If my weekly reflection gets missed for some reason, this is how often I'll retry so it doesn't just fall through the cracks.",
    "agent_calendar": "The calendar (create it once in Calendar.app) where I'll put every task block I schedule for you, kept visually separate from your own events.",
    "calendars_include": "Only want me looking at specific calendars? List them here. Leave it blank and I'll consider all of them, minus whatever you exclude below.",
    "calendars_exclude": "Calendars I should never touch or even closely look at. This always overrides the include list.",
    "reminder_lists_include": "Only want me pulling tasks from certain Reminders lists? List them here — otherwise I'll consider all your lists.",
    "reminder_lists_exclude": "Reminders lists I should completely ignore when filling your day with tasks.",
    "always_fixed_hints": "Keywords that, if they appear in an event's title, guarantee I treat it as a locked commitment — never something I'll fill, resize, or move.",
    "goal_fill_weight_staleness": "How much I favor a goal simply because it's been sitting neglected. Turn this up and I'll rotate faster onto whatever goal you haven't touched in a while.",
    "goal_fill_weight_priority": "How much I favor a goal because you marked it more important. Turn this up and I'll lean toward your top-priority goals even if you worked on them recently.",
    "goal_fill_max_minutes_per_goal": "The most time in one day I'll spend on a single goal — past this, I'll switch to your next goal instead of dumping everything on one thing.",
    "reminder_intake_list": "The Reminders.app list I'll file a new reminder into when you text it to me directly — must already exist, I won't create it myself.",
    "reminder_intake_timeout_minutes": "If I ask a clarifying question about a reminder you texted me and you go quiet, I'll drop that conversation after this long so a later unrelated text doesn't get mixed up with it.",
}

# Protected-buffer rows are indexed (protected_buffer_match_0, _1, ...) so they can't use
# the name-keyed _HELP lookup above — every row shares this same explanation instead.
_PROTECTED_MATCH_HELP = "Any of these words, matched against the event's title or calendar name, marks it as protected — I'll never fill it with a task or shrink it. Blank rows below are just open slots for adding a new one."
_PROTECTED_GOAL_FILL_HELP = "If checked, I can still tuck a bit of goal work into this time — just never an ordinary to-do."

_STYLE = """
:root {
  --ink: #1c1b22;
  --ink-soft: #57545f;
  --bg: #faf7f2;
  --surface: #ffffff;
  --surface-tint: #eef0fd;
  --primary: #6366f1;
  --primary-soft: rgba(99, 102, 241, 0.16);
  --secondary: #10b981;
  --secondary-hover: #0ea371;
  --secondary-ink: #06281c;
  --accent: #f59e0b;
  --danger: #e11d48;
  --danger-bg: #fde8ec;
  --border: #e7e2d8;
  --border-strong: #d9d3c5;
  --radius-card: 20px;
  --radius-control: 10px;
  --radius-pill: 999px;
  --glass-bg: rgba(250, 247, 242, 0.78);
  --shadow-card: 0 1px 2px rgba(28, 27, 34, 0.04), 0 8px 24px rgba(28, 27, 34, 0.06);
  --shadow-card-hover: 0 4px 10px rgba(28, 27, 34, 0.07), 0 18px 40px rgba(28, 27, 34, 0.10);
  --shadow-pop: 0 10px 30px rgba(28, 27, 34, 0.16);
  --ease: cubic-bezier(0.22, 1, 0.36, 1);
}
* { box-sizing: border-box; }
html, body { background: var(--bg); }
body {
  font-family: Inter, -apple-system, system-ui, "Segoe UI", Helvetica, Arial, sans-serif;
  color: var(--ink);
  margin: 0;
  padding-bottom: 2rem;
  position: relative;
}
body::before {
  content: "";
  position: fixed; z-index: -1; top: -25%; left: 50%; width: 900px; height: 900px;
  transform: translateX(-50%);
  background:
    radial-gradient(circle at 30% 30%, rgba(99, 102, 241, 0.18), transparent 60%),
    radial-gradient(circle at 70% 60%, rgba(16, 185, 129, 0.13), transparent 55%);
  filter: blur(50px);
  animation: drift 20s ease-in-out infinite alternate;
}
@keyframes drift {
  from { transform: translateX(-50%) translateY(0) scale(1); }
  to { transform: translateX(-47%) translateY(30px) scale(1.06); }
}
@media (prefers-reduced-motion: reduce) { body::before { animation: none; } }
h1, h2 { margin: 0; }
p { margin: 0; }

.page-header {
  position: sticky; top: 0; z-index: 30;
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(14px);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(99, 102, 241, 0.12);
}
.page-header__inner { max-width: 1120px; margin: 0 auto; padding: 1.5rem 2rem 1rem; }
.eyebrow {
  display: inline-block; background: var(--ink); color: #fff;
  font-size: 12px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
  border-radius: var(--radius-pill); padding: 4px 12px; margin-bottom: 8px;
}
.page-header h1 {
  font-size: 28px; font-weight: 800; letter-spacing: -0.02em;
  background: linear-gradient(90deg, var(--ink) 0%, var(--primary) 100%);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}

.grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 1.5rem;
  max-width: 1120px;
  margin: 2rem auto;
  padding: 0 2rem;
}
.card {
  grid-column: span 12;
  background: var(--surface);
  border: 1.5px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  padding: 1.5rem;
  transition: transform 0.3s var(--ease), box-shadow 0.3s var(--ease), border-color 0.3s var(--ease);
  animation: card-in 0.45s var(--ease) both;
}
.card:hover, .card:focus-within {
  transform: translateY(-4px);
  box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.35), var(--shadow-card-hover);
  border-color: var(--primary);
}
.card--hero { background: var(--surface-tint); }
@keyframes card-in { from { opacity: 0; } to { opacity: 1; } }
.grid .card:nth-child(1) { animation-delay: 0.02s; }
.grid .card:nth-child(2) { animation-delay: 0.07s; }
.grid .card:nth-child(3) { animation-delay: 0.12s; }
.grid .card:nth-child(4) { animation-delay: 0.17s; }
.grid .card:nth-child(5) { animation-delay: 0.22s; }
.grid .card:nth-child(6) { animation-delay: 0.27s; }
.grid .card:nth-child(7) { animation-delay: 0.32s; }
.grid .card:nth-child(8) { animation-delay: 0.37s; }
@media (prefers-reduced-motion: reduce) { .card { animation: none; } }
@media (min-width: 860px) {
  .card--span-3 { grid-column: span 3; }
  .card--span-4 { grid-column: span 4; }
  .card--span-5 { grid-column: span 5; }
  .card--span-6 { grid-column: span 6; }
  .card--span-7 { grid-column: span 7; }
  .card--span-8 { grid-column: span 8; }
  .card--span-12 { grid-column: span 12; }
}
.card__head { margin-bottom: 1.25rem; }
.card__head h2 { font-size: 19px; font-weight: 800; }
.card__subtitle { font-size: 14px; color: var(--ink-soft); margin-top: 2px; }

.field { margin-bottom: 1rem; }
.field:last-child { margin-bottom: 0; }
.field--full { grid-column: 1 / -1; }
.field-grid-2 { display: grid; grid-template-columns: 1fr; gap: 1rem; margin-bottom: 1rem; }
@media (min-width: 640px) { .field-grid-2 { grid-template-columns: 1fr 1fr; } }

.field__label-row { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
label { font-size: 14px; font-weight: 700; color: var(--ink-soft); }

input[type=text], input[type=number], input[type=time], textarea {
  display: block; width: 100%; padding: 10px 12px;
  border: 1.5px solid var(--border-strong); border-radius: var(--radius-control);
  font: inherit; font-size: 15px; background: var(--surface); color: var(--ink);
  transition: border-color 0.2s var(--ease), box-shadow 0.2s var(--ease);
}
textarea { font-family: ui-monospace, monospace; resize: none; overflow-y: hidden; }
input[type=number] { font-variant-numeric: tabular-nums; letter-spacing: 0.02em; }
input:focus-visible, textarea:focus-visible {
  outline: none; border-color: var(--primary); box-shadow: 0 0 0 4px var(--primary-soft);
}
summary:focus-visible, button:focus-visible {
  outline: 2px solid var(--primary); outline-offset: 2px;
}

.help { position: relative; display: inline-flex; cursor: help; }
.help__badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); color: #3d2b06; font-size: 12px; font-weight: 800;
  box-shadow: 0 1px 2px rgba(28, 27, 34, 0.18);
  transition: transform 0.2s var(--ease), box-shadow 0.2s var(--ease), background 0.2s var(--ease);
}
.help:hover .help__badge, .help:focus-visible .help__badge, .help:focus .help__badge {
  transform: scale(1.12);
  background: var(--primary); color: #fff;
  box-shadow: 0 0 0 4px var(--primary-soft), 0 3px 10px rgba(99, 102, 241, 0.35);
}
.help__pop {
  position: absolute; z-index: 40; top: calc(100% + 8px); left: 50%;
  transform: translate(-50%, -4px);
  width: max-content; max-width: min(260px, 80vw);
  background: var(--ink); color: #fff; border-radius: 10px;
  padding: 10px 12px; font-size: 13px; line-height: 1.45; font-weight: 400;
  box-shadow: var(--shadow-pop);
  opacity: 0; visibility: hidden; pointer-events: none;
  transition: opacity 0.2s var(--ease), transform 0.2s var(--ease), visibility 0.2s;
}
.help:hover .help__pop, .help:focus-visible .help__pop, .help:focus .help__pop {
  opacity: 1; visibility: visible; transform: translate(-50%, 0); pointer-events: auto;
}
@media (prefers-reduced-motion: reduce) { .help__pop { transition: opacity 0.05s linear; } }

.advanced { margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.75rem; }
.advanced > summary {
  cursor: pointer; font-size: 13px; font-weight: 700; color: var(--ink-soft);
  transition: color 0.2s var(--ease);
}
.advanced > summary:hover { color: var(--primary); }
.advanced .field { margin-top: 0.75rem; }

.weight-pair { display: flex; align-items: flex-end; gap: 12px; }
.weight-pair .field { flex: 1; margin-bottom: 0; }
.weight-pair__op { font-size: 20px; font-weight: 800; padding-bottom: 10px; color: var(--ink-soft); }
.weight-sum-bar { display: flex; height: 10px; border-radius: 999px; overflow: hidden; background: var(--border); margin-top: 12px; }
.weight-sum-bar span { transition: width 0.3s var(--ease); }
.weight-sum-bar span:first-child { background: var(--primary); }
.weight-sum-bar span:last-child { background: var(--accent); }
.weight-sum-text { font-size: 13px; color: var(--ink-soft); margin-top: 6px; }

.callout {
  background: var(--surface-tint); border: 1.5px solid var(--border); border-radius: var(--radius-control);
  padding: 10px 12px; font-size: 13px; color: var(--ink-soft); margin-top: 1rem;
}

.buffer-row { border-top: 1px solid var(--border); padding-top: 1rem; margin-top: 1rem; }
.buffer-row:first-child { border-top: none; margin-top: 0; padding-top: 0; }
.buffer-row label { display: block; margin-bottom: 4px; font-size: 14px; font-weight: 700; color: var(--ink-soft); }
.buffer-row textarea { margin-bottom: 8px; }
.checkbox-line { display: flex; align-items: center; gap: 8px; }
.checkbox-line input { width: auto; accent-color: var(--primary); }
.checkbox-line label { margin: 0; font-weight: 600; }

.banner {
  border: 1.5px solid transparent; border-radius: var(--radius-control);
  padding: 10px 14px; font-size: 14px; font-weight: 600; margin-top: 12px;
  transition: opacity 0.2s var(--ease);
}
.banner.ok { background: rgba(16, 185, 129, 0.12); border-color: rgba(16, 185, 129, 0.35); color: var(--secondary-ink); }
.banner.error { background: var(--danger-bg); border-color: rgba(225, 29, 72, 0.35); color: #7a0d28; }

.save-bar {
  position: sticky; bottom: 0; z-index: 30;
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(14px);
  backdrop-filter: blur(14px);
  border-top: 1px solid var(--border);
  margin-top: 1rem;
}
.save-bar__inner {
  max-width: 1120px; margin: 0 auto; padding: 1rem 2rem;
  display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap;
}
.save-bar__hint { font-size: 13px; color: var(--ink-soft); }
.btn-save {
  font: inherit; font-size: 16px; font-weight: 800; padding: 14px 28px;
  border: none; border-radius: var(--radius-pill);
  background: var(--secondary); color: var(--secondary-ink);
  box-shadow: 0 2px 4px rgba(16, 185, 129, 0.25), 0 10px 24px rgba(16, 185, 129, 0.18);
  cursor: pointer;
  transition: transform 0.25s var(--ease), box-shadow 0.25s var(--ease), background 0.25s var(--ease);
  animation: pulse-glow 3.5s ease-in-out infinite;
}
@keyframes pulse-glow {
  0%, 100% { box-shadow: 0 2px 4px rgba(16, 185, 129, 0.25), 0 10px 24px rgba(16, 185, 129, 0.18); }
  50% { box-shadow: 0 2px 4px rgba(16, 185, 129, 0.32), 0 12px 30px rgba(16, 185, 129, 0.28); }
}
.btn-save:hover {
  transform: translateY(-2px); background: var(--secondary-hover);
  box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.18), 0 4px 8px rgba(16, 185, 129, 0.3), 0 16px 32px rgba(16, 185, 129, 0.22);
  animation: none;
}
.btn-save:active { transform: translateY(0) scale(0.98); box-shadow: 0 2px 4px rgba(16, 185, 129, 0.2); animation: none; }
@media (prefers-reduced-motion: reduce) { .btn-save { animation: none; } }
"""


def _get_nested(raw: dict, *keys, default=""):
    d = raw
    for k in keys[:-1]:
        d = (d or {}).get(k) or {}
    return d.get(keys[-1], default) if isinstance(d, dict) else default


# _LIST_FIELDS uses rules.yaml's flat form-field names, but calendars/reminder_lists are
# actually nested one level down in the YAML (e.g. calendars: {include, exclude}) — this
# maps the flat name to its real nested path so existing values pre-fill correctly.
_LIST_FIELD_NESTED_PATH = {
    "calendars_include": ("calendars", "include"),
    "calendars_exclude": ("calendars", "exclude"),
    "reminder_lists_include": ("reminder_lists", "include"),
    "reminder_lists_exclude": ("reminder_lists", "exclude"),
}


def _list_field_values(raw: dict, name: str) -> list:
    path = _LIST_FIELD_NESTED_PATH.get(name)
    if path:
        return _get_nested(raw, *path, default=[]) or []
    return raw.get(name) or []


def _textarea_rows(values: list, min_rows: int = 2, max_rows: int = 10) -> int:
    """Initial size so existing content is visible without scrolling on first paint —
    the `data-autogrow` script then keeps it growing as the user types more lines."""
    return max(min_rows, min(max_rows, len(values) + 1))


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _help(name: str, text: Optional[str] = None) -> str:
    text = text if text is not None else _HELP.get(name)
    if not text:
        return ""
    return (
        f'<span class="help" tabindex="0" role="button" aria-label="What does this do?">'
        f'<span class="help__badge" aria-hidden="true">?</span>'
        f'<span class="help__pop">{_esc(text)}</span>'
        f"</span>"
    )


def _field(name: str, label: str, control_html: str, extra_class: str = "", help_text: Optional[str] = None) -> str:
    classes = " ".join(c for c in ("field", extra_class) if c)
    return f"""
    <div class="{classes}">
      <div class="field__label-row"><label for="{name}">{_esc(label)}</label>{_help(name, help_text)}</div>
      {control_html}
    </div>"""


def _card(title: str, subtitle: str, body: str, span: str = "", extra_class: str = "") -> str:
    classes = " ".join(c for c in ("card", span, extra_class) if c)
    return f"""
    <div class="{classes}">
      <div class="card__head">
        <h2>{_esc(title)}</h2>
        <p class="card__subtitle">{_esc(subtitle)}</p>
      </div>
      {body}
    </div>"""


def _number_field_html(raw: dict, name: str) -> str:
    label, default, step = _NUMBER_FIELDS_BY_NAME[name]
    control = f'<input type="number" step="{step}" id="{name}" name="{name}" value="{_esc(raw.get(name, default))}">'
    return _field(name, label, control)


def _number_fields_html(raw: dict, names: tuple) -> str:
    return "".join(_number_field_html(raw, name) for name in names)


def _time_field(raw: dict, name: str, label: str, default: str) -> str:
    control = f'<input type="time" id="{name}" name="{name}" value="{_esc(raw.get(name, default))}">'
    return _field(name, label, control)


def _goal_fill_weights_pct(raw: dict) -> tuple:
    try:
        staleness = float(_get_nested(raw, "goal_fill", "weight_staleness", default=0.6))
        priority = float(_get_nested(raw, "goal_fill", "weight_priority", default=0.4))
    except (TypeError, ValueError):
        return 0.0, 50.0, 50.0
    total = staleness + priority
    if total <= 0:
        return total, 50.0, 50.0
    return total, staleness / total * 100, priority / total * 100


def render_settings_form(raw: dict, token: str, error: Optional[str] = None, saved: bool = False) -> str:
    protected_buffers = list(raw.get("protected_buffers") or [])
    buffer_rows = "".join(
        _render_buffer_row(i, pb)
        for i, pb in enumerate(protected_buffers + [{}] * _EXTRA_PROTECTED_BUFFER_SLOTS)
    )

    banner = ""
    if error:
        banner = f'<div class="banner error">Not saved — {_esc(error)}</div>'
    elif saved:
        banner = '<div class="banner ok">Locked in. Takes effect on the next scheduler tick — no restart needed.</div>'

    morning_evening_body = (
        _time_field(raw, "morning_checkin_floor", "Morning check-in floor", "09:00")
        + _time_field(raw, "evening_checkin_floor", "Evening check-in floor", "22:00")
        + _number_fields_html(raw, ("day_boundary_gap_hours", "floor_confirmation_retry_minutes"))
    )

    agent_calendar_body = _field(
        "agent_calendar",
        "Agent-authored blocks calendar",
        f'<input type="text" id="agent_calendar" name="agent_calendar" value="{_esc(raw.get("agent_calendar", ""))}">',
    )

    timing_body = (
        _number_fields_html(raw, ("followup_delay_unexpected_minutes", "followup_delay_continuing_minutes"))
        + '<details class="advanced"><summary>Advanced</summary>'
        + _number_fields_html(raw, ("safety_backstop_minutes",))
        + "</details>"
    )

    sizing_body = _number_fields_html(raw, ("min_block_minutes", "buffer_block_minutes"))

    review_body = _number_fields_html(raw, ("weekly_review_interval_days", "weekly_review_retry_minutes"))

    scope_pairs_html = "".join(
        _field(
            name, label,
            f'<textarea id="{name}" name="{name}" rows="{_textarea_rows(_list_field_values(raw, name), min_rows=3)}" '
            f'placeholder="{_esc(hint)}" data-autogrow>{_esc(chr(10).join(_list_field_values(raw, name)))}</textarea>',
        )
        for name, label, hint in _LIST_FIELDS[:4]
    )
    _afh_name, _afh_label, _afh_hint = _LIST_FIELDS[4]
    _afh_values = _list_field_values(raw, _afh_name)
    scope_hints_html = _field(
        _afh_name, _afh_label,
        f'<textarea id="{_afh_name}" name="{_afh_name}" rows="{_textarea_rows(_afh_values, min_rows=2)}" '
        f'placeholder="{_esc(_afh_hint)}" data-autogrow>{_esc(chr(10).join(_afh_values))}</textarea>',
        extra_class="field--full",
    )
    scope_body = f'<div class="field-grid-2">{scope_pairs_html}</div>{scope_hints_html}'

    staleness_val = _get_nested(raw, "goal_fill", "weight_staleness", default=0.6)
    priority_val = _get_nested(raw, "goal_fill", "weight_priority", default=0.4)
    total, staleness_pct, priority_pct = _goal_fill_weights_pct(raw)
    goal_fill_body = (
        '<div class="weight-pair">'
        + _field(
            "goal_fill_weight_staleness", "Staleness weight",
            f'<input type="number" step="0.05" min="0" max="1" id="goal_fill_weight_staleness" name="goal_fill_weight_staleness" value="{_esc(staleness_val)}">',
        )
        + '<span class="weight-pair__op" aria-hidden="true">+</span>'
        + _field(
            "goal_fill_weight_priority", "Priority weight",
            f'<input type="number" step="0.05" min="0" max="1" id="goal_fill_weight_priority" name="goal_fill_weight_priority" value="{_esc(priority_val)}">',
        )
        + "</div>"
        + f'<div class="weight-sum-bar"><span style="width:{staleness_pct:.1f}%"></span><span style="width:{priority_pct:.1f}%"></span></div>'
        + f'<p class="weight-sum-text">Right now these add up to {total:.2f} — does not need to be exactly 1.0, but that is the sweet spot.</p>'
        + _field(
            "goal_fill_max_minutes_per_goal", "Max minutes per goal (per session)",
            f'<input type="number" step="1" id="goal_fill_max_minutes_per_goal" name="goal_fill_max_minutes_per_goal" value="{_esc(_get_nested(raw, "goal_fill", "max_minutes_per_goal", default=180))}">',
        )
    )

    protected_time_body = (
        buffer_rows
        + '<p class="callout">Keep a row matching goal time, or the Goal Time bucket '
        + "feature (from the day-start Light, Balanced, Heavy prompt) turns off.</p>"
    )

    reminder_intake_body = _field(
        "reminder_intake_list",
        "Reminders list for texted-in reminders",
        f'<input type="text" id="reminder_intake_list" name="reminder_intake_list" '
        f'value="{_esc(raw.get("reminder_intake_list", ""))}">',
    ) + _number_fields_html(raw, ("reminder_intake_timeout_minutes",))

    cards = "".join([
        _card("Morning & Evening",
              "When your day quietly begins and ends, as far as I'm concerned.",
              morning_evening_body, span="card--span-8"),
        _card("Agent Calendar", "Where I write down every block I schedule for you.",
              agent_calendar_body, span="card--span-4", extra_class="card--hero"),
        _card("Timing & Retries", "How patient I am, and how soon I circle back.",
              timing_body, span="card--span-6"),
        _card("Task Sizing", "How small a task can be before it's not worth its own block.",
              sizing_body, span="card--span-3"),
        _card("Weekly Reflection", "How often I look back and learn.",
              review_body, span="card--span-3"),
        _card("What I Pay Attention To", "Which calendars, lists, and keywords I pay attention to.",
              scope_body, span="card--span-12"),
        _card("Choosing Your Goal", "How I decide which goal gets your free time when there's open goal time in the day.",
              goal_fill_body, span="card--span-5"),
        _card("Protected Time", "The moments I'll never touch.",
              protected_time_body, span="card--span-7"),
        _card("Reminder Intake", "Texting me a reminder directly, instead of typing it into Reminders yourself.",
              reminder_intake_body, span="card--span-5"),
    ])

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Crono.Ai Settings</title>
<style>
{_STYLE}
</style>
</head>
<body>
<form method="post" action="/settings">
  <input type="hidden" name="token" value="{_esc(token)}">
  <div class="page-header">
    <div class="page-header__inner">
      <span class="eyebrow">Settings</span>
      <h1>Crono.Ai</h1>
      {banner}
    </div>
  </div>

  <div class="grid">
    {cards}
  </div>

  <div class="save-bar">
    <div class="save-bar__inner">
      <p class="save-bar__hint">Saves straight to rules.yaml — takes effect on the next scheduler tick, no restart needed.</p>
      <button type="submit" class="btn-save">Lock it in!</button>
    </div>
  </div>
</form>
<script>
(function () {{
  function autosize(el) {{ el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; }}
  document.querySelectorAll("textarea[data-autogrow]").forEach(function (el) {{
    autosize(el);
    el.addEventListener("input", function () {{ autosize(el); }});
  }});
}})();
</script>
</body>
</html>"""


def _render_buffer_row(i: int, pb: dict) -> str:
    match_lines = pb.get("match") or []
    match_text = "\n".join(match_lines)
    checked = "checked" if pb.get("allow_goal_fill", True) else ""
    match_name = f"protected_buffer_match_{i}"
    goal_fill_name = f"protected_buffer_allow_goal_fill_{i}"
    match_field = _field(
        match_name, "Match keywords (one per line)",
        f'<textarea id="{match_name}" name="{match_name}" rows="{_textarea_rows(match_lines, min_rows=2)}" data-autogrow>{_esc(match_text)}</textarea>',
        help_text=_PROTECTED_MATCH_HELP,
    )
    return f"""
    <div class="buffer-row">
      {match_field}
      <div class="checkbox-line">
        <input type="checkbox" id="pb_goal_fill_{i}" name="{goal_fill_name}" {checked}>
        <label for="pb_goal_fill_{i}">Allow goal-fill here</label>
        {_help(goal_fill_name, _PROTECTED_GOAL_FILL_HELP)}
      </div>
    </div>"""


def parse_form_to_rules_dict(form) -> dict:
    """Inverse of render_settings_form — rebuilds the nested dict shape
    config._rules_from_dict expects from submitted form fields. `form` just needs a
    `.get(key, default)` method (Starlette's FormData already provides this)."""

    def _lines(name: str) -> list[str]:
        raw_text = form.get(name, "") or ""
        return [line.strip() for line in raw_text.splitlines() if line.strip()]

    def _number(value: str, kind):
        # Best-effort cast so a well-formed value round-trips as a real YAML number
        # instead of a quoted string. A bad value is left as-is — config._rules_from_dict
        # is the single source of truth for rejecting it with a friendly error.
        try:
            return kind(value)
        except (TypeError, ValueError):
            return value

    raw: dict = {
        "morning_checkin_floor": form.get("morning_checkin_floor", ""),
        "evening_checkin_floor": form.get("evening_checkin_floor", ""),
        "agent_calendar": form.get("agent_calendar", ""),
        "reminder_intake_list": form.get("reminder_intake_list", ""),
        "calendars": {
            "include": _lines("calendars_include"),
            "exclude": _lines("calendars_exclude"),
        },
        "reminder_lists": {
            "include": _lines("reminder_lists_include"),
            "exclude": _lines("reminder_lists_exclude"),
        },
        "always_fixed_hints": _lines("always_fixed_hints"),
        "goal_fill": {
            "weight_staleness": _number(form.get("goal_fill_weight_staleness", "0.6"), float),
            "weight_priority": _number(form.get("goal_fill_weight_priority", "0.4"), float),
            "max_minutes_per_goal": _number(form.get("goal_fill_max_minutes_per_goal", "180"), int),
        },
    }

    for name, _label, default, step in _NUMBER_FIELDS:
        # The HTML `step` already distinguishes float fields ("any") from whole-number
        # ones ("1") — reuse it instead of a second parallel type list.
        kind = float if step == "any" else int
        raw[name] = _number(form.get(name, str(default)), kind)

    protected_buffers = []
    i = 0
    while True:
        match_key = f"protected_buffer_match_{i}"
        if form.get(match_key) is None:
            break
        match_lines = _lines(match_key)
        if match_lines:
            protected_buffers.append({
                "match": match_lines,
                "allow_goal_fill": form.get(f"protected_buffer_allow_goal_fill_{i}") is not None,
            })
        i += 1
    raw["protected_buffers"] = protected_buffers

    return raw
