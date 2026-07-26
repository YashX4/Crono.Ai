"""Tier C — needs the full env-var-before-import dance (imports timeblock_agent.orchestrator).
Automated coverage for conversational reminder intake via Telegram (ROADMAP.md B.1,
rules.md §10) — the clear-task path, the one-clarifying-question loop, timeout expiry, and
Cancel. Cases A/B/D-ii are model-driven (retry(3, ...), same non-determinism-tolerant
pattern used elsewhere in this suite for real-model calls) but hit a free LOCAL Ollama
model (see reminder_intake.py) rather than the billed Anthropic API — requires `ollama
serve` running locally with OLLAMA_MODEL (default qwen2.5:14b) already pulled. Case C stubs
reminder_intake.resolve() directly since the timeout mechanism itself is pure code, nothing
to do with model behavior.

Usage: .venv/bin/python fake_sandbox/test_scenario_reminder_intake.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TraceLogger, make_capturing_wrapper, retry, setup_fake_env  # noqa: E402

_tmp_dir = Path(tempfile.mkdtemp(prefix="crono-test-reminder-intake-"))
setup_fake_env(_tmp_dir)

from fake_eventkit_bridge import FakeEventKitBridge  # noqa: E402
from timeblock_agent import orchestrator, telegram_notifier  # noqa: E402
from timeblock_agent.config import load_rules  # noqa: E402
from timeblock_agent.orchestrator import RULES_PATH  # noqa: E402
from timeblock_agent.reminder_intake import ReminderIntakeResult  # noqa: E402
from timeblock_agent.state import AgentState, PendingReminderIntake, load_state, save_state  # noqa: E402

trace = TraceLogger("test_scenario_reminder_intake")

_NOW = datetime(2026, 7, 26, 10, 0)  # a Sunday — never ambiguous with "the coming Friday"


def _fresh_bridge(tag: str) -> FakeEventKitBridge:
    return FakeEventKitBridge(store_path=_tmp_dir / f"eventkit_{tag}_{id(object())}.json")


def _fake_confirm_sender():
    """Telegram is deliberately unconfigured in this sandbox (setup_fake_env blanks the
    creds), so the real notifier.send_reminder_confirmation always returns None — no
    token to track. This stand-in mirrors the real telegram_notifier function's actual
    token bookkeeping (invalidate the stale one, register a fresh one via the REAL
    `_register_callback`/`invalidate_callback`/`_pending`) without the network send, so
    orchestrator's token-tracking/revision/invalidation logic gets exercised for real."""
    calls: list[dict] = []

    def sender(title, notes, due_date, previous_token=None):
        if previous_token:
            telegram_notifier.invalidate_callback(previous_token)
        token = telegram_notifier._register_callback(
            {
                "kind": "reminder_confirm", "title": title, "notes": notes,
                "due_date": due_date.isoformat() if due_date else None,
            }
        )
        calls.append({"args": (title, notes, due_date), "result": token})
        return token

    return sender, calls


def case_a_clear_task():
    save_state(AgentState())
    bridge = _fresh_bridge("a")
    rules = load_rules(RULES_PATH)

    sender, calls = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender):
        trace.call("handle_reminder_message", text="call the dentist friday", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "call the dentist friday", now=_NOW)

    trace.check("send_reminder_confirmation called exactly once (resolved straight to ready)", 1, len(calls))
    title, notes, due_date = calls[0]["args"]
    trace.check_true("title looks like a dentist reminder", "dentist" in title.lower(), detail=title)

    state = load_state()
    trace.check_true(
        "thread stays open with an active confirm token (revisable until a real tap)",
        state.pending_reminder_intake is not None and state.pending_reminder_intake.active_confirm_token is not None,
    )

    trace.check_true("a due date was resolved", due_date is not None)
    trace.check("resolved due date is a Friday", 4, due_date.weekday())
    trace.check_true("resolved due date is today or later", due_date.date() >= _NOW.date())

    trace.call("handle_reminder_confirmation", answer="yes", title=title)
    orchestrator.handle_reminder_confirmation(bridge, "yes", title, notes, due_date, now=_NOW)
    reminders = bridge.list_reminders(list_titles=[rules.reminder_intake_list])
    trace.check_true(
        f"reminder {title!r} was created in the {rules.reminder_intake_list!r} list",
        any(r.title == title for r in reminders),
    )

    state = load_state()
    trace.check("pending thread closed once the real tap resolves it", None, state.pending_reminder_intake)


def case_b_clarify_then_resolve():
    save_state(AgentState())
    bridge = _fresh_bridge("b")
    rules = load_rules(RULES_PATH)

    notify_wrapper, notify_calls = make_capturing_wrapper(orchestrator.send_notification)
    sender, confirm_calls = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_notification", side_effect=notify_wrapper), \
         mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender):
        trace.call("handle_reminder_message", text="remind me to do that thing", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "remind me to do that thing", now=_NOW)

    state = load_state()
    trace.check_true("pending thread opened for a vague task", state.pending_reminder_intake is not None)
    trace.check("exactly one plain clarifying question sent", 1, len(notify_calls))
    trace.check("no confirm-with-buttons sent yet", 0, len(confirm_calls))
    reminders = bridge.list_reminders(list_titles=[rules.reminder_intake_list])
    trace.check("nothing written to Reminders yet", 0, len(reminders))

    sender2, confirm_calls2 = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender2):
        trace.call("handle_reminder_message", text="call my mom", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "call my mom", now=_NOW)

    trace.check("confirmation sent after the concrete follow-up", 1, len(confirm_calls2))
    state = load_state()
    trace.check_true(
        "thread stays open (revisable) rather than closing itself",
        state.pending_reminder_intake is not None and state.pending_reminder_intake.active_confirm_token is not None,
    )

    title2, notes2, due_date2 = confirm_calls2[0]["args"]
    orchestrator.handle_reminder_confirmation(bridge, "yes", title2, notes2, due_date2, now=_NOW)
    state = load_state()
    trace.check("pending thread closed once the real tap resolves it", None, state.pending_reminder_intake)
    reminders = bridge.list_reminders(list_titles=[rules.reminder_intake_list])
    trace.check_true(
        f"reminder {title2!r} was created after confirming the follow-up",
        any(r.title == title2 for r in reminders),
    )


def case_c_timeout_expiry():
    save_state(AgentState())
    bridge = _fresh_bridge("c")
    rules = load_rules(RULES_PATH)
    trace.note(f"reminder_intake_timeout_minutes = {rules.reminder_intake_timeout_minutes}")

    # Stale thread (older than the timeout) — must be dropped before the next message
    # reaches the model, so the model only ever sees the fresh, unrelated text.
    state = load_state()
    state.pending_reminder_intake = PendingReminderIntake(
        messages=[
            {"role": "user", "content": "remind me to do that thing"},
            {"role": "assistant", "content": "What thing?"},
        ],
        started_at=_NOW - timedelta(minutes=40),
        last_activity_at=_NOW - timedelta(minutes=rules.reminder_intake_timeout_minutes + 1),
    )
    save_state(state)

    captured: dict = {}

    def fake_resolve_stale(messages, now):
        captured["messages"] = messages
        return ReminderIntakeResult(action="clarify", clarifying_question="placeholder")

    with mock.patch("timeblock_agent.reminder_intake.resolve", side_effect=fake_resolve_stale):
        trace.call("handle_reminder_message", text="totally unrelated new text", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "totally unrelated new text", now=_NOW)

    trace.check("stale thread dropped: model only saw the one new message", 1, len(captured["messages"]))
    trace.check(
        "the one message the model saw is the new text, not the stale thread",
        "totally unrelated new text", captured["messages"][0]["content"],
    )

    # Boundary companion: just inside the timeout — the thread must be preserved.
    state = load_state()
    state.pending_reminder_intake = PendingReminderIntake(
        messages=[{"role": "user", "content": "remind me to do that thing"}],
        started_at=_NOW - timedelta(minutes=rules.reminder_intake_timeout_minutes - 1),
        last_activity_at=_NOW - timedelta(minutes=rules.reminder_intake_timeout_minutes - 1),
    )
    save_state(state)

    captured2: dict = {}

    def fake_resolve_fresh(messages, now):
        captured2["messages"] = messages
        return ReminderIntakeResult(action="clarify", clarifying_question="placeholder2")

    with mock.patch("timeblock_agent.reminder_intake.resolve", side_effect=fake_resolve_fresh):
        trace.call("handle_reminder_message", text="a follow-up reply", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "a follow-up reply", now=_NOW)

    trace.check(
        "thread just inside the timeout preserved: model saw prior turn plus the new one",
        2, len(captured2["messages"]),
    )


def case_d_cancel_at_confirmation():
    save_state(AgentState())
    bridge = _fresh_bridge("d1")
    rules = load_rules(RULES_PATH)

    trace.call("handle_reminder_confirmation", answer="cancel", title="Some reminder")
    orchestrator.handle_reminder_confirmation(bridge, "cancel", "Some reminder", now=_NOW)
    reminders = bridge.list_reminders(list_titles=[rules.reminder_intake_list])
    trace.check("cancel at the confirmation step writes nothing", 0, len(reminders))


def case_d_cancel_mid_conversation():
    save_state(AgentState())
    bridge = _fresh_bridge("d2")
    rules = load_rules(RULES_PATH)

    trace.call("handle_reminder_message", text="remind me to do that thing", now=_NOW.isoformat())
    orchestrator.handle_reminder_message(bridge, "remind me to do that thing", now=_NOW)
    state = load_state()
    trace.check_true("clarify thread opened before the cancel", state.pending_reminder_intake is not None)

    trace.call("handle_reminder_message", text="never mind, forget it", now=_NOW.isoformat())
    orchestrator.handle_reminder_message(bridge, "never mind, forget it", now=_NOW)
    state = load_state()
    trace.check("pending thread cleared after a plain-language cancel", None, state.pending_reminder_intake)
    reminders = bridge.list_reminders(list_titles=[rules.reminder_intake_list])
    trace.check("nothing written after a mid-conversation cancel", 0, len(reminders))


def case_e_revise_after_confirmation():
    """Live-caught behavior (not in the original design doc, added after a real Telegram
    session): pushing back on a shown confirmation ("actually make it monday instead")
    must revise the same draft, not open an unrelated new request — and the superseded
    confirmation's buttons must stop working."""
    save_state(AgentState())
    bridge = _fresh_bridge("e")
    rules = load_rules(RULES_PATH)

    sender1, calls1 = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender1):
        trace.call("handle_reminder_message", text="remind me to call the dentist friday", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "remind me to call the dentist friday", now=_NOW)

    trace.check("initial confirmation sent", 1, len(calls1))
    first_token = calls1[0]["result"]
    trace.check_true("a real token was issued for the first confirmation", first_token is not None)
    state = load_state()
    trace.check(
        "state tracks that same token as the active one",
        first_token, state.pending_reminder_intake.active_confirm_token,
    )

    sender2, calls2 = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender2):
        trace.call("handle_reminder_message", text="actually make it monday instead", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "actually make it monday instead", now=_NOW)

    trace.check("pushback revised the draft with a new confirmation, not a new request", 1, len(calls2))
    _, _, revised_due_date = calls2[0]["args"]
    trace.check_true("revised due date was resolved", revised_due_date is not None)
    trace.check("revised due date is a Monday", 0, revised_due_date.weekday())

    trace.check_true(
        "the superseded first token no longer resolves (stale tap can't act)",
        telegram_notifier.resolve_callback(first_token) is None,
    )

    second_token = calls2[0]["result"]
    state = load_state()
    trace.check(
        "state now tracks the SECOND token as active",
        second_token, state.pending_reminder_intake.active_confirm_token,
    )

    title2, notes2, due_date2 = calls2[0]["args"]
    orchestrator.handle_reminder_confirmation(bridge, "yes", title2, notes2, due_date2, now=_NOW)
    reminders = bridge.list_reminders(list_titles=[rules.reminder_intake_list])
    trace.check_true(
        "the reminder was created with the REVISED (Monday) due date, not the original Friday one",
        any(r.title == title2 and r.due_date and r.due_date.weekday() == 0 for r in reminders),
    )
    state = load_state()
    trace.check("pending thread closed after the real tap", None, state.pending_reminder_intake)


def case_f_topic_shift_no_contamination():
    """Live-caught behavior (user's exact scenario): an unconfirmed draft ("book the
    dentist at 5"), followed a couple minutes later by an unrelated request ("book a
    restaurant"), must not get blended together — the new request resolves on its own,
    and the old draft's already-shown confirmation is left completely untouched (still
    tappable later), not invalidated, since nothing said to drop it."""
    save_state(AgentState())
    bridge = _fresh_bridge("f")

    sender1, calls1 = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender1):
        trace.call("handle_reminder_message", text="can you book the dentist at 5", now=_NOW.isoformat())
        orchestrator.handle_reminder_message(bridge, "can you book the dentist at 5", now=_NOW)

    trace.check("first (dentist) confirmation sent", 1, len(calls1))
    first_token = calls1[0]["result"]
    trace.check_true("a real token was issued for the dentist confirmation", first_token is not None)

    later = _NOW + timedelta(minutes=2)
    notify_wrapper, notify_calls = make_capturing_wrapper(orchestrator.send_notification)
    sender2, calls2 = _fake_confirm_sender()
    with mock.patch("timeblock_agent.orchestrator.send_notification", side_effect=notify_wrapper), \
         mock.patch("timeblock_agent.orchestrator.send_reminder_confirmation", side_effect=sender2):
        trace.call("handle_reminder_message", text="can I book a restaurant", now=later.isoformat())
        orchestrator.handle_reminder_message(bridge, "can I book a restaurant", now=later)

    # Whichever action it resolved to (a clean "ready", or a clarifying question because
    # "book a restaurant" alone still felt underspecified) — the important invariant is
    # zero trace of the still-unconfirmed dentist thread anywhere in the response.
    if calls2:
        response_text = calls2[0]["args"][0]  # the confirmation's title
    else:
        trace.check("exactly one clarifying notification sent for the restaurant message", 1, len(notify_calls))
        response_text = notify_calls[0]["args"][2]  # send_notification(kind, title, text) -> the question
    trace.check_true(
        "no dentist contamination in the response to the topic switch",
        "dentist" not in response_text.lower(), detail=response_text,
    )
    trace.check_true(
        "response is actually about the restaurant", "restaurant" in response_text.lower(), detail=response_text,
    )
    trace.check_true(
        "the OLD (dentist) token is untouched by the topic switch — still valid",
        telegram_notifier.resolve_callback(first_token) is not None,
    )

    state = load_state()
    trace.check_true(
        "the new thread's own stored messages carry no dentist text either",
        all("dentist" not in (m.get("content") or "").lower() for m in state.pending_reminder_intake.messages),
    )


def main():
    ok = True
    try:
        retry(3, case_a_clear_task, trace, "Case A: clear task")
        retry(3, case_b_clarify_then_resolve, trace, "Case B: one clarifying-question loop")
        case_c_timeout_expiry()
        case_d_cancel_at_confirmation()
        retry(3, case_d_cancel_mid_conversation, trace, "Case D: mid-conversation cancel")
        retry(3, case_e_revise_after_confirmation, trace, "Case E: revise after confirmation")
        retry(3, case_f_topic_shift_no_contamination, trace, "Case F: topic shift, no contamination")
        print("Reminder intake scenario: PASS")
    except AssertionError as e:
        ok = False
        print(f"FAILED: {e}")
        raise
    finally:
        trace.finish(ok)
    print(f"\nAll cases passed. Trace log: {trace.path}")


if __name__ == "__main__":
    main()
