"""Loads and validates rules.yaml into typed config objects.

Raises ConfigError with a clear message on anything malformed or missing —
callers should treat that as fatal (no-op / refuse to run), not a warning.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Union

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

_yaml_roundtrip = YAML()
_yaml_roundtrip.preserve_quotes = True


class ConfigError(RuntimeError):
    pass


@dataclass
class ProtectedBuffer:
    match: list[str]
    allow_goal_fill: bool = True


@dataclass
class GoalFillWeights:
    staleness: float = 0.6
    priority: float = 0.4
    max_minutes_per_goal: int = 180


@dataclass
class Rules:
    morning_checkin_floor: time
    evening_checkin_floor: time
    day_boundary_gap_hours: float
    floor_confirmation_retry_minutes: int
    safety_backstop_minutes: int
    followup_delay_unexpected_minutes: int
    followup_delay_continuing_minutes: int
    min_block_minutes: int
    buffer_block_minutes: int
    agent_calendar: str
    calendars_include: list[str]
    calendars_exclude: list[str]
    reminder_lists_include: list[str]
    reminder_lists_exclude: list[str]
    protected_buffers: list[ProtectedBuffer]
    always_fixed_hints: list[str]
    goal_fill_weights: GoalFillWeights
    weekly_review_interval_days: float
    weekly_review_retry_minutes: int
    reminder_intake_list: str
    reminder_intake_timeout_minutes: int


def _parse_time(value, field_name: str) -> time:
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a \"HH:MM\" string, got {value!r}")
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as e:
        raise ConfigError(f"Invalid time for {field_name}: {value!r} (expected HH:MM)") from e


def _require(raw: dict, key: str):
    if key not in raw or raw[key] is None:
        raise ConfigError(f"rules.yaml missing required field: {key}")
    return raw[key]


def _int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from None


def _float(raw: dict, key: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a number, got {value!r}") from None


def load_rules(path: Union[Path, str] = "rules.yaml") -> Rules:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Rules file not found: {path}")

    with open(path) as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Malformed rules.yaml: {e}") from e

    return _rules_from_dict(raw)


def _rules_from_dict(raw: dict) -> Rules:
    """The actual dict-to-Rules parsing/validation, split out of load_rules so a caller
    with an in-memory dict (see server.py's settings-page POST handler) can validate a
    submission through the exact same rules load_rules itself trusts, before ever
    writing anything to disk — rather than duplicating this logic or round-tripping
    through a temp file just to reuse it."""
    if not isinstance(raw, dict):
        raise ConfigError("rules.yaml must be a mapping at the top level")

    protected_buffers = []
    for p in raw.get("protected_buffers") or []:
        if "match" not in p:
            raise ConfigError("Each protected_buffers entry needs a `match` list")
        protected_buffers.append(
            ProtectedBuffer(match=p["match"], allow_goal_fill=p.get("allow_goal_fill", True))
        )

    gf = raw.get("goal_fill") or {}
    calendars = raw.get("calendars") or {}
    reminder_lists_cfg = raw.get("reminder_lists") or {}

    return Rules(
        morning_checkin_floor=_parse_time(_require(raw, "morning_checkin_floor"), "morning_checkin_floor"),
        evening_checkin_floor=_parse_time(_require(raw, "evening_checkin_floor"), "evening_checkin_floor"),
        day_boundary_gap_hours=_float(raw, "day_boundary_gap_hours", 4),
        floor_confirmation_retry_minutes=_int(raw, "floor_confirmation_retry_minutes", 30),
        safety_backstop_minutes=_int(raw, "safety_backstop_minutes", 60),
        followup_delay_unexpected_minutes=_int(raw, "followup_delay_unexpected_minutes", 30),
        followup_delay_continuing_minutes=_int(raw, "followup_delay_continuing_minutes", 60),
        min_block_minutes=_int(raw, "min_block_minutes", 15),
        buffer_block_minutes=_int(raw, "buffer_block_minutes", 15),
        agent_calendar=_require(raw, "agent_calendar"),
        calendars_include=calendars.get("include", []),
        calendars_exclude=calendars.get("exclude", []),
        reminder_lists_include=reminder_lists_cfg.get("include", []),
        reminder_lists_exclude=reminder_lists_cfg.get("exclude", []),
        protected_buffers=protected_buffers,
        always_fixed_hints=raw.get("always_fixed_hints", []),
        goal_fill_weights=GoalFillWeights(
            staleness=_float(gf, "weight_staleness", 0.6),
            priority=_float(gf, "weight_priority", 0.4),
            max_minutes_per_goal=_int(gf, "max_minutes_per_goal", 180),
        ),
        weekly_review_interval_days=_float(raw, "weekly_review_interval_days", 7),
        weekly_review_retry_minutes=_int(raw, "weekly_review_retry_minutes", 60),
        reminder_intake_list=_require(raw, "reminder_intake_list"),
        reminder_intake_timeout_minutes=_int(raw, "reminder_intake_timeout_minutes", 30),
    )


_SCALAR_TIME_KEYS = ("morning_checkin_floor", "evening_checkin_floor")
_TOP_LEVEL_KEYS = (
    "morning_checkin_floor", "evening_checkin_floor", "agent_calendar", "always_fixed_hints",
    "day_boundary_gap_hours", "floor_confirmation_retry_minutes", "safety_backstop_minutes",
    "followup_delay_unexpected_minutes", "followup_delay_continuing_minutes",
    "min_block_minutes", "buffer_block_minutes", "weekly_review_interval_days",
    "weekly_review_retry_minutes", "reminder_intake_list", "reminder_intake_timeout_minutes",
)
_NESTED_MAP_KEYS = ("calendars", "reminder_lists", "goal_fill")


def apply_rules_dict(existing_text: str, new_raw: dict) -> str:
    """Merges a validated settings-form dict (settings_page.parse_form_to_rules_dict)
    into the existing rules.yaml text IN PLACE, editing only the keys the form actually
    covers. A plain `yaml.safe_dump(new_raw)` of a fresh dict would silently discard
    every comment in the file (rules.yaml's own header docs, inline notes on individual
    settings) and re-quote every scalar to PyYAML's default style — this preserves both,
    the same way a human hand-editing the file would."""
    doc = _yaml_roundtrip.load(existing_text)
    if not isinstance(doc, CommentedMap):
        doc = CommentedMap()

    for key in _TOP_LEVEL_KEYS:
        if key not in new_raw:
            continue
        value = new_raw[key]
        if key in _SCALAR_TIME_KEYS:
            # Quoted explicitly: an unquoted HH:MM like 00:00 risks being read back as a
            # YAML 1.1 sexagesimal integer by a plain (non-round-trip) loader.
            value = DoubleQuotedScalarString(str(value))
        doc[key] = value

    if "protected_buffers" in new_raw:
        # A comment sitting between `protected_buffers` and the next key (e.g. rules.yaml's
        # "Classification hints" note above `always_fixed_hints`) isn't anchored to the
        # map or to the sequence — ruamel attaches it to the LAST dict item's LAST key
        # (here, `allow_goal_fill` of the last protected buffer). The form always rebuilds
        # this list from scratch as plain dicts, so that anchor has to be captured before
        # the old items are discarded and transplanted onto the new last item, or the
        # comment silently vanishes.
        existing_seq = doc.get("protected_buffers")
        trailing_ca_items = None
        if isinstance(existing_seq, CommentedSeq) and len(existing_seq) > 0:
            last_item = existing_seq[-1]
            if isinstance(last_item, CommentedMap) and last_item.ca.items:
                trailing_ca_items = dict(last_item.ca.items)

        if not isinstance(existing_seq, CommentedSeq):
            existing_seq = CommentedSeq()
            doc["protected_buffers"] = existing_seq
        del existing_seq[:]
        new_items = [CommentedMap(item) for item in new_raw["protected_buffers"]]
        existing_seq.extend(new_items)

        if trailing_ca_items and new_items:
            new_items[-1].ca.items.update(trailing_ca_items)

    for nested_key in _NESTED_MAP_KEYS:
        if nested_key not in new_raw:
            continue
        existing_nested = doc.get(nested_key)
        if not isinstance(existing_nested, CommentedMap):
            existing_nested = CommentedMap()
            doc[nested_key] = existing_nested
        for sub_key, sub_value in new_raw[nested_key].items():
            existing_nested[sub_key] = sub_value

    buf = io.StringIO()
    _yaml_roundtrip.dump(doc, buf)
    return buf.getvalue()
