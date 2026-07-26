"""Thin wrapper over pyobjc's EventKit bindings for Calendar + Reminders access.

Faithful read/write layer only — no FIXED/FLEXIBLE classification, no agent
tagging convention, no scheduling logic. Those live in higher-level modules
that consume the plain dataclasses returned here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import EventKit
from AppKit import NSColorSpace
from Foundation import (
    NSCalendar,
    NSCalendarUnitDay,
    NSCalendarUnitHour,
    NSCalendarUnitMinute,
    NSCalendarUnitMonth,
    NSCalendarUnitYear,
    NSDate,
    NSRunLoop,
)


class EventKitError(RuntimeError):
    pass


class AccessDeniedError(EventKitError):
    pass


@dataclass
class CalendarInfo:
    identifier: str
    title: str
    allows_modifications: bool
    color: Optional[str]  # hex string, e.g. "#RRGGBB", or None if unavailable


@dataclass
class CalendarEvent:
    identifier: str
    title: str
    start: datetime
    end: datetime
    is_all_day: bool
    calendar_identifier: str
    calendar_title: str
    location: Optional[str]
    notes: Optional[str]
    url: Optional[str]
    has_attendees: bool


@dataclass
class Reminder:
    identifier: str
    title: str
    notes: Optional[str]
    due_date: Optional[datetime]
    priority: int  # EventKit scale: 0 = none, 1-4 = high, 5 = medium, 6-9 = low
    completed: bool
    list_identifier: str
    list_title: str


def _to_nsdate(dt: datetime) -> NSDate:
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _from_nsdate(nsdate) -> Optional[datetime]:
    if nsdate is None:
        return None
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970())


def _pump_run_loop_until(is_done, timeout: float = 15.0) -> None:
    deadline = NSDate.dateWithTimeIntervalSinceNow_(timeout)
    run_loop = NSRunLoop.currentRunLoop()
    while not is_done() and NSDate.date().compare_(deadline) != 1:  # 1 == NSOrderedDescending
        run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    if not is_done():
        raise EventKitError(f"Timed out after {timeout}s waiting on EventKit callback")


def _color_to_hex(ns_color) -> Optional[str]:
    if ns_color is None:
        return None
    try:
        rgb = ns_color.colorUsingColorSpace_(NSColorSpace.genericRGBColorSpace())
        r, g, b = int(rgb.redComponent() * 255), int(rgb.greenComponent() * 255), int(rgb.blueComponent() * 255)
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return None


class EventKitBridge:
    def __init__(self):
        self.store = EventKit.EKEventStore.alloc().init()

    # ---- Access ----

    def request_access(self, timeout: float = 30.0) -> tuple[bool, bool]:
        """Prompts (on first run) for Calendar + Reminders access. Blocks until resolved."""
        cal_result: list = []
        rem_result: list = []

        if hasattr(self.store, "requestFullAccessToEventsWithCompletion_"):
            self.store.requestFullAccessToEventsWithCompletion_(
                lambda granted, error: cal_result.append(granted)
            )
        else:
            self.store.requestAccessToEntityType_completion_(
                EventKit.EKEntityTypeEvent, lambda granted, error: cal_result.append(granted)
            )

        if hasattr(self.store, "requestFullAccessToRemindersWithCompletion_"):
            self.store.requestFullAccessToRemindersWithCompletion_(
                lambda granted, error: rem_result.append(granted)
            )
        else:
            self.store.requestAccessToEntityType_completion_(
                EventKit.EKEntityTypeReminder, lambda granted, error: rem_result.append(granted)
            )

        _pump_run_loop_until(lambda: cal_result and rem_result, timeout=timeout)
        return bool(cal_result[0]), bool(rem_result[0])

    # ---- Calendars ----

    def list_calendars(self, entity: str = "event") -> list[CalendarInfo]:
        entity_type = EventKit.EKEntityTypeEvent if entity == "event" else EventKit.EKEntityTypeReminder
        calendars = self.store.calendarsForEntityType_(entity_type)
        return [
            CalendarInfo(
                identifier=cal.calendarIdentifier(),
                title=cal.title(),
                allows_modifications=cal.allowsContentModifications(),
                color=_color_to_hex(cal.color()),
            )
            for cal in calendars
        ]

    def _find_calendar(self, title: str, entity: str = "event"):
        for cal in self.store.calendarsForEntityType_(
            EventKit.EKEntityTypeEvent if entity == "event" else EventKit.EKEntityTypeReminder
        ):
            if cal.title() == title:
                return cal
        raise EventKitError(f"No {entity} calendar named {title!r}")

    # ---- Events (Calendar) ----

    def _event_to_dataclass(self, ev) -> CalendarEvent:
        cal = ev.calendar()
        return CalendarEvent(
            identifier=ev.eventIdentifier(),
            title=ev.title() or "",
            start=_from_nsdate(ev.startDate()),
            end=_from_nsdate(ev.endDate()),
            is_all_day=bool(ev.isAllDay()),
            calendar_identifier=cal.calendarIdentifier() if cal else "",
            calendar_title=cal.title() if cal else "",
            location=ev.location() or None,
            notes=ev.notes() or None,
            url=str(ev.URL()) if ev.URL() else None,
            has_attendees=bool(ev.hasAttendees()),
        )

    def list_events(
        self, start: datetime, end: datetime, calendar_titles: Optional[list[str]] = None
    ) -> list[CalendarEvent]:
        calendars = None
        if calendar_titles:
            all_cals = self.store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
            calendars = [c for c in all_cals if c.title() in calendar_titles]
        predicate = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            _to_nsdate(start), _to_nsdate(end), calendars
        )
        events = self.store.eventsMatchingPredicate_(predicate)
        return [self._event_to_dataclass(ev) for ev in events]

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        calendar_title: str,
        notes: Optional[str] = None,
        location: Optional[str] = None,
    ) -> str:
        event = EventKit.EKEvent.eventWithEventStore_(self.store)
        event.setTitle_(title)
        event.setStartDate_(_to_nsdate(start))
        event.setEndDate_(_to_nsdate(end))
        event.setCalendar_(self._find_calendar(calendar_title, entity="event"))
        if notes is not None:
            event.setNotes_(notes)
        if location is not None:
            event.setLocation_(location)
        ok, error = self.store.saveEvent_span_error_(event, EventKit.EKSpanThisEvent, None)
        if not ok:
            raise EventKitError(f"Failed to save event: {error}")
        return event.eventIdentifier()

    def update_event(
        self,
        identifier: str,
        title: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        notes: Optional[str] = None,
        location: Optional[str] = None,
    ) -> None:
        event = self.store.eventWithIdentifier_(identifier)
        if event is None:
            raise EventKitError(f"No event with identifier {identifier}")
        if title is not None:
            event.setTitle_(title)
        if start is not None:
            event.setStartDate_(_to_nsdate(start))
        if end is not None:
            event.setEndDate_(_to_nsdate(end))
        if notes is not None:
            event.setNotes_(notes)
        if location is not None:
            event.setLocation_(location)
        ok, error = self.store.saveEvent_span_error_(event, EventKit.EKSpanThisEvent, None)
        if not ok:
            raise EventKitError(f"Failed to update event: {error}")

    def delete_event(self, identifier: str) -> None:
        event = self.store.eventWithIdentifier_(identifier)
        if event is None:
            raise EventKitError(f"No event with identifier {identifier}")
        ok, error = self.store.removeEvent_span_error_(event, EventKit.EKSpanThisEvent, None)
        if not ok:
            raise EventKitError(f"Failed to delete event: {error}")

    # ---- Reminders ----

    def _reminder_to_dataclass(self, rem) -> Reminder:
        cal = rem.calendar()
        due = rem.dueDateComponents()
        due_dt = None
        if due is not None:
            gregorian = NSCalendar.currentCalendar()
            nsdate = gregorian.dateFromComponents_(due)
            due_dt = _from_nsdate(nsdate)
        return Reminder(
            identifier=rem.calendarItemIdentifier(),
            title=rem.title() or "",
            notes=rem.notes() or None,
            due_date=due_dt,
            priority=int(rem.priority()),
            completed=bool(rem.isCompleted()),
            list_identifier=cal.calendarIdentifier() if cal else "",
            list_title=cal.title() if cal else "",
        )

    def list_reminders(
        self, include_completed: bool = False, list_titles: Optional[list[str]] = None, timeout: float = 15.0
    ) -> list[Reminder]:
        calendars = None
        if list_titles:
            all_cals = self.store.calendarsForEntityType_(EventKit.EKEntityTypeReminder)
            calendars = [c for c in all_cals if c.title() in list_titles]

        if include_completed:
            predicate = self.store.predicateForRemindersInCalendars_(calendars)
        else:
            predicate = self.store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                None, None, calendars
            )

        result: list = []

        def handler(reminders):
            result.append(reminders or [])

        self.store.fetchRemindersMatchingPredicate_completion_(predicate, handler)
        _pump_run_loop_until(lambda: len(result) > 0, timeout=timeout)
        return [self._reminder_to_dataclass(r) for r in result[0]]

    def create_reminder(
        self,
        title: str,
        list_title: str,
        due_date: Optional[datetime] = None,
        notes: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        reminder = EventKit.EKReminder.reminderWithEventStore_(self.store)
        reminder.setTitle_(title)
        reminder.setCalendar_(self._find_calendar(list_title, entity="reminder"))
        if notes is not None:
            reminder.setNotes_(notes)
        if priority:
            reminder.setPriority_(priority)
        if due_date is not None:
            gregorian = NSCalendar.currentCalendar()
            units = (
                NSCalendarUnitYear
                | NSCalendarUnitMonth
                | NSCalendarUnitDay
                | NSCalendarUnitHour
                | NSCalendarUnitMinute
            )
            components = gregorian.components_fromDate_(units, _to_nsdate(due_date))
            reminder.setDueDateComponents_(components)
        ok, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not ok:
            raise EventKitError(f"Failed to save reminder: {error}")
        return reminder.calendarItemIdentifier()

    def complete_reminder(self, identifier: str) -> None:
        reminder = self.store.calendarItemWithIdentifier_(identifier)
        if reminder is None:
            raise EventKitError(f"No reminder with identifier {identifier}")
        reminder.setCompleted_(True)
        ok, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not ok:
            raise EventKitError(f"Failed to complete reminder: {error}")

    def delete_reminder(self, identifier: str) -> None:
        reminder = self.store.calendarItemWithIdentifier_(identifier)
        if reminder is None:
            raise EventKitError(f"No reminder with identifier {identifier}")
        ok, error = self.store.removeReminder_commit_error_(reminder, True, None)
        if not ok:
            raise EventKitError(f"Failed to delete reminder: {error}")
