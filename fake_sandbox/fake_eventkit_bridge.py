"""In-memory, JSON-persisted stand-in for `timeblock_agent.eventkit_bridge.EventKitBridge`
— duck-types the same 11-method public surface so every caller (`orchestrator.py`,
`diff_write.py`, `day_layout.py`, `classify.py`, `incremental_replan.py`, and the
`scripts/` test helpers) works against it with zero changes, since nothing in this
codebase ever does a Protocol/ABC/isinstance check on the bridge it's handed.

Reuses `CalendarEvent`/`Reminder`/`CalendarInfo` directly from `eventkit_bridge.py`
(never redefines them) — those are already plain str/bool/int/datetime dataclasses with
no live EventKit object references, so they round-trip through JSON cleanly.

Two fidelity requirements matter more than anything else here, both tied directly to
real bugs already found testing against the real bridge:
  - `list_events` filters by real interval-overlap (`ev.start < end and ev.end > start`),
    not containment or start-only — this is exactly the semantic behind bug #24 (a query
    starting at `now` silently missing a block whose end had already passed).
  - `create_reminder`'s `due_date` truncates to minute precision, matching real
    EventKit's own date-component granularity.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from timeblock_agent.eventkit_bridge import CalendarEvent, CalendarInfo, EventKitError, Reminder

FAKE_EVENTKIT_PATH = Path(
    os.environ.get("TIMEBLOCK_FAKE_EVENTKIT_PATH", str(Path.home() / ".timeblock-agent-fake" / "fake_eventkit.json"))
)


def _event_to_dict(ev: CalendarEvent) -> dict:
    return {
        "identifier": ev.identifier,
        "title": ev.title,
        "start": ev.start.isoformat(),
        "end": ev.end.isoformat(),
        "is_all_day": ev.is_all_day,
        "calendar_identifier": ev.calendar_identifier,
        "calendar_title": ev.calendar_title,
        "location": ev.location,
        "notes": ev.notes,
        "url": ev.url,
        "has_attendees": ev.has_attendees,
    }


def _event_from_dict(d: dict) -> CalendarEvent:
    return CalendarEvent(
        identifier=d["identifier"],
        title=d["title"],
        start=datetime.fromisoformat(d["start"]),
        end=datetime.fromisoformat(d["end"]),
        is_all_day=d["is_all_day"],
        calendar_identifier=d["calendar_identifier"],
        calendar_title=d["calendar_title"],
        location=d["location"],
        notes=d["notes"],
        url=d["url"],
        has_attendees=d["has_attendees"],
    )


def _reminder_to_dict(r: Reminder) -> dict:
    return {
        "identifier": r.identifier,
        "title": r.title,
        "notes": r.notes,
        "due_date": r.due_date.isoformat() if r.due_date else None,
        "priority": r.priority,
        "completed": r.completed,
        "list_identifier": r.list_identifier,
        "list_title": r.list_title,
    }


def _reminder_from_dict(d: dict) -> Reminder:
    return Reminder(
        identifier=d["identifier"],
        title=d["title"],
        notes=d["notes"],
        due_date=datetime.fromisoformat(d["due_date"]) if d["due_date"] else None,
        priority=d["priority"],
        completed=d["completed"],
        list_identifier=d["list_identifier"],
        list_title=d["list_title"],
    )


def _calendar_to_dict(c: CalendarInfo) -> dict:
    return {"identifier": c.identifier, "title": c.title, "allows_modifications": c.allows_modifications, "color": c.color}


def _calendar_from_dict(d: dict) -> CalendarInfo:
    return CalendarInfo(**d)


class FakeEventKitBridge:
    """Drop-in replacement for `EventKitBridge` backed by plain dicts instead of a real
    `EKEventStore` — no Calendar.app/Reminders.app, no TCC prompt, no wall-clock coupling
    (the caller supplies every `datetime` explicitly, same as the real bridge)."""

    def __init__(self, store_path: Optional[Path] = None):
        self._store_path = store_path or FAKE_EVENTKIT_PATH
        self._events: dict[str, CalendarEvent] = {}
        self._reminders: dict[str, Reminder] = {}
        self._event_calendars: dict[str, CalendarInfo] = {}
        self._reminder_calendars: dict[str, CalendarInfo] = {}
        self._load()

    # ---- Persistence ----

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._events = {k: _event_from_dict(v) for k, v in data.get("events", {}).items()}
        self._reminders = {k: _reminder_from_dict(v) for k, v in data.get("reminders", {}).items()}
        self._event_calendars = {k: _calendar_from_dict(v) for k, v in data.get("event_calendars", {}).items()}
        self._reminder_calendars = {k: _calendar_from_dict(v) for k, v in data.get("reminder_calendars", {}).items()}

    def _save(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "events": {k: _event_to_dict(v) for k, v in self._events.items()},
            "reminders": {k: _reminder_to_dict(v) for k, v in self._reminders.items()},
            "event_calendars": {k: _calendar_to_dict(v) for k, v in self._event_calendars.items()},
            "reminder_calendars": {k: _calendar_to_dict(v) for k, v in self._reminder_calendars.items()},
        }
        self._store_path.write_text(json.dumps(data, indent=2))

    # ---- Access ----

    def request_access(self, timeout: float = 30.0) -> tuple[bool, bool]:
        return (True, True)

    # ---- Calendars ----

    def _get_or_create_calendar(self, title: str, entity: str) -> CalendarInfo:
        store = self._event_calendars if entity == "event" else self._reminder_calendars
        cal = store.get(title)
        if cal is None:
            cal = CalendarInfo(
                identifier=f"FAKE-CAL-{uuid.uuid4().hex}", title=title, allows_modifications=True, color=None
            )
            store[title] = cal
        return cal

    def list_calendars(self, entity: str = "event") -> list[CalendarInfo]:
        store = self._event_calendars if entity == "event" else self._reminder_calendars
        return list(store.values())

    # ---- Events (Calendar) ----

    def list_events(
        self, start: datetime, end: datetime, calendar_titles: Optional[list[str]] = None
    ) -> list[CalendarEvent]:
        return [
            ev
            for ev in self._events.values()
            if ev.start < end
            and ev.end > start
            and (calendar_titles is None or ev.calendar_title in calendar_titles)
        ]

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        calendar_title: str,
        notes: Optional[str] = None,
        location: Optional[str] = None,
    ) -> str:
        cal = self._get_or_create_calendar(calendar_title, entity="event")
        identifier = f"FAKE-{uuid.uuid4().hex}"
        self._events[identifier] = CalendarEvent(
            identifier=identifier,
            title=title,
            start=start,
            end=end,
            is_all_day=False,
            calendar_identifier=cal.identifier,
            calendar_title=calendar_title,
            location=location,
            notes=notes,
            url=None,
            has_attendees=False,
        )
        self._save()
        return identifier

    def update_event(
        self,
        identifier: str,
        title: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        notes: Optional[str] = None,
        location: Optional[str] = None,
    ) -> None:
        existing = self._events.get(identifier)
        if existing is None:
            raise EventKitError(f"No event with identifier {identifier}")
        if title is not None:
            existing.title = title
        if start is not None:
            existing.start = start
        if end is not None:
            existing.end = end
        if notes is not None:
            existing.notes = notes
        if location is not None:
            existing.location = location
        self._save()

    def delete_event(self, identifier: str) -> None:
        if identifier not in self._events:
            raise EventKitError(f"No event with identifier {identifier}")
        del self._events[identifier]
        self._save()

    # ---- Reminders ----

    def list_reminders(
        self, include_completed: bool = False, list_titles: Optional[list[str]] = None, timeout: float = 15.0
    ) -> list[Reminder]:
        return [
            r
            for r in self._reminders.values()
            if (include_completed or not r.completed) and (list_titles is None or r.list_title in list_titles)
        ]

    def create_reminder(
        self,
        title: str,
        list_title: str,
        due_date: Optional[datetime] = None,
        notes: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        cal = self._get_or_create_calendar(list_title, entity="reminder")
        identifier = f"FAKE-{uuid.uuid4().hex}"
        self._reminders[identifier] = Reminder(
            identifier=identifier,
            title=title,
            notes=notes,
            due_date=due_date.replace(second=0, microsecond=0) if due_date is not None else None,
            priority=priority,
            completed=False,
            list_identifier=cal.identifier,
            list_title=list_title,
        )
        self._save()
        return identifier

    def complete_reminder(self, identifier: str) -> None:
        reminder = self._reminders.get(identifier)
        if reminder is None:
            raise EventKitError(f"No reminder with identifier {identifier}")
        reminder.completed = True
        self._save()

    def delete_reminder(self, identifier: str) -> None:
        if identifier not in self._reminders:
            raise EventKitError(f"No reminder with identifier {identifier}")
        del self._reminders[identifier]
        self._save()
