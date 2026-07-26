"""Manual smoke test: requests Calendar + Reminders access, then lists what it can see.

Run with: .venv/bin/python scripts/check_access.py
First run will trigger macOS permission dialogs — approve both.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeblock_agent.eventkit_bridge import EventKitBridge


def main():
    bridge = EventKitBridge()

    print("Requesting Calendar + Reminders access...")
    cal_granted, rem_granted = bridge.request_access(timeout=120)
    print(f"  Calendar access: {'granted' if cal_granted else 'DENIED'}")
    print(f"  Reminders access: {'granted' if rem_granted else 'DENIED'}")

    if not cal_granted and not rem_granted:
        print("\nNo access granted. Check System Settings > Privacy & Security > Calendars/Reminders.")
        return

    if cal_granted:
        print("\nCalendars:")
        for cal in bridge.list_calendars(entity="event"):
            mod = "writable" if cal.allows_modifications else "read-only"
            print(f"  - {cal.title} ({mod}, color={cal.color})")

        start = datetime.now()
        end = start + timedelta(days=1)
        print(f"\nEvents in next 24h ({start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}):")
        events = bridge.list_events(start, end)
        if not events:
            print("  (none)")
        for ev in events:
            attendee_flag = " [attendees]" if ev.has_attendees else ""
            loc_flag = f" @ {ev.location}" if ev.location else ""
            print(f"  - {ev.start:%H:%M}-{ev.end:%H:%M} {ev.title!r} ({ev.calendar_title}){attendee_flag}{loc_flag}")

    if rem_granted:
        print("\nReminder lists:")
        for cal in bridge.list_calendars(entity="reminder"):
            mod = "writable" if cal.allows_modifications else "read-only"
            print(f"  - {cal.title} ({mod})")

        print("\nOpen reminders:")
        reminders = bridge.list_reminders(include_completed=False)
        if not reminders:
            print("  (none)")
        for rem in reminders:
            due = f" (due {rem.due_date:%Y-%m-%d %H:%M})" if rem.due_date else ""
            print(f"  - {rem.title!r} [{rem.list_title}] priority={rem.priority}{due}")


if __name__ == "__main__":
    main()
