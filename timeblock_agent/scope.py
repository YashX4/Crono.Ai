"""Calendar/reminder-list scoping from rules.yaml's include/exclude lists. Exclude always wins."""

from __future__ import annotations

from timeblock_agent.config import Rules


def in_calendar_scope(calendar_title: str, rules: Rules) -> bool:
    if rules.calendars_exclude and calendar_title in rules.calendars_exclude:
        return False
    if rules.calendars_include and calendar_title not in rules.calendars_include:
        return False
    return True


def in_reminder_list_scope(list_title: str, rules: Rules) -> bool:
    if rules.reminder_lists_exclude and list_title in rules.reminder_lists_exclude:
        return False
    if rules.reminder_lists_include and list_title not in rules.reminder_lists_include:
        return False
    return True
