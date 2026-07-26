"""Persistent scheduler + webhook process (see spec: "Notification delivery" / "Startup /
always-running"). Single process — merges the webhook and the dynamic scheduler per the
decisions log ("Webhook uptime: merged into the single persistent scheduler process").

Design note / deviation from the spec's stack: this uses asyncio's own scheduling
(create_task + sleep) rather than APScheduler's thread-based scheduler. EventKit access
from a background thread is untested and risky in pyobjc — every live script this build
has used ran on the main thread. Keeping every EventKit/Claude call on the single asyncio
event-loop thread (blocking it briefly, a few seconds per cycle) is a safer trade-off for
a single-user local tool than risking flaky Calendar access from a worker thread.

Notifications arrive back via Telegram button taps (long-polling — see
_telegram_poll_loop — Telegram is the notification transport, chosen over Pushcut
specifically so the callback direction works regardless of which network the phone is on;
long-polling means the Mac reaches OUT to Telegram's servers rather than needing an
inbound connection reachable from the phone). asyncio.to_thread is safe for the poll
loop's blocking wait specifically because it's a plain HTTP call with no EventKit/pyobjc
involvement — unlike the EventKit-touching code above, which never moves off the main
thread.

Run with: .venv/bin/python -m uvicorn timeblock_agent.server:app --host 0.0.0.0 --port 8787
Must run from Terminal.app (not VS Code) for Calendar/Reminders access.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from timeblock_agent import orchestrator, settings_page, telegram_notifier, wake_schedule
from timeblock_agent.config import ConfigError, _rules_from_dict, apply_rules_dict, load_rules
from timeblock_agent.eventkit_bridge import EventKitBridge
from timeblock_agent.state import Trigger, load_state, save_state

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("timeblock_agent.server")

# httpx logs the full URL of every request at INFO level. Telegram's API (like Pushcut's
# before it) embeds the auth secret directly in the URL path rather than a header, so that
# default logging leaks the bot token into these logs verbatim — confirmed live once
# already. Suppressing httpx's own logger (not ours) is the actual fix, independent of
# which future integration also happens to put a secret in a URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN")
DEDUP_WINDOW_SECONDS = 60
MIN_TICK_DELAY_SECONDS = 5  # independent circuit breaker: see state.py's compute_next_trigger
# docstring — this bug class (a stale candidate time never advancing) has already shown up
# twice via two different trigger reasons. This floor means even an undiscovered third
# variant degrades to "ticks every 5s" (loud, obvious, cheap to notice in logs) rather than
# a silent multi-hour tight loop burning API calls and spamming notifications overnight.

_bridge: Optional[EventKitBridge] = None
_current_task: Optional[asyncio.Task] = None
_telegram_poll_task: Optional[asyncio.Task] = None
_next_trigger: Optional[Trigger] = None
_recent_dedup: dict[str, datetime] = {}  # dedup key -> when we last started processing it


def _check_token(token: str) -> None:
    if not WEBHOOK_TOKEN:
        raise HTTPException(status_code=500, detail="WEBHOOK_TOKEN not configured on the server")
    if not token or not hmac.compare_digest(token, WEBHOOK_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


def _is_duplicate(key: str) -> bool:
    """Shared dedup guard for both the HTTP webhook and Telegram callback paths — a
    replan can take 10-20+ seconds (two sequential Claude calls), long enough that an
    impatient second tap (or a redelivered Telegram update) is a real occurrence, not a
    hypothetical."""
    now = datetime.now()
    for k, seen_at in list(_recent_dedup.items()):
        if (now - seen_at).total_seconds() > 300:
            del _recent_dedup[k]

    last_seen = _recent_dedup.get(key)
    if last_seen and (now - last_seen).total_seconds() < DEDUP_WINDOW_SECONDS:
        return True
    _recent_dedup[key] = now
    return False


async def _run_tick() -> None:
    global _next_trigger, _current_task
    if load_state().agent_paused:
        # Persisted, not just in-memory — a restart (LaunchAgent auto-restart, reboot)
        # must come back up still paused rather than silently resuming, matching what
        # the menu bar's "Agent Active" toggle actually promises. POST /resume flips
        # this to False BEFORE calling _run_tick, so the real resume path is unaffected.
        logger.info("Agent is paused — skipping tick (POST /resume to reactivate)")
        _current_task = None
        return
    wake_schedule.caffeinate_briefly()
    try:
        trigger = orchestrator.run_scheduled_tick(_bridge)
    except Exception:
        logger.exception("Scheduled tick failed — falling back to safety backstop retry")
        rules = load_rules(orchestrator.RULES_PATH)
        trigger = Trigger(datetime.now() + timedelta(minutes=rules.safety_backstop_minutes), "error_fallback")

    _next_trigger = trigger
    wake_schedule.schedule_wake(trigger.at)
    logger.info("Next trigger: %s at %s", trigger.reason, trigger.at)
    _current_task = asyncio.create_task(_sleep_then_tick())


async def _sleep_then_tick() -> None:
    delay = max((_next_trigger.at - datetime.now()).total_seconds(), MIN_TICK_DELAY_SECONDS)
    await asyncio.sleep(delay)
    await _run_tick()


async def _reschedule_after(trigger: Trigger) -> None:
    global _next_trigger, _current_task
    _next_trigger = trigger
    if _current_task and not _current_task.done():
        _current_task.cancel()
    if load_state().agent_paused:
        # A genuine incoming answer (e.g. a stray Telegram tap on a notification sent
        # before pausing) still gets processed by the caller for correctness — nothing
        # silently dropped — but must never re-arm the automatic tick loop itself, or
        # "Agent Active: off" wouldn't actually mean off.
        logger.info(
            "Agent is paused — processed the answer but not re-arming (next would have "
            "been %s at %s)", trigger.reason, trigger.at,
        )
        _current_task = None
        return
    wake_schedule.schedule_wake(trigger.at)
    _current_task = asyncio.create_task(_sleep_then_tick())


async def _process_checkin_answer(block_id: str, answer: str) -> None:
    wake_schedule.caffeinate_briefly()
    try:
        trigger = orchestrator.run_checkin_answer(_bridge, block_id, answer)
    except Exception:
        logger.exception("Failed to process check-in answer for %s", block_id)
        return
    logger.info("Processed check-in for %s (%s) -> next trigger %s at %s", block_id, answer, trigger.reason, trigger.at)
    await _reschedule_after(trigger)


async def _process_day_boundary(event: str, answer: str) -> None:
    wake_schedule.caffeinate_briefly()
    try:
        trigger = orchestrator.handle_day_boundary(_bridge, event, answer)
    except Exception:
        logger.exception("Failed to process day boundary %s (%s)", event, answer)
        return
    logger.info("Processed day boundary %s (%s) -> next trigger %s at %s", event, answer, trigger.reason, trigger.at)
    await _reschedule_after(trigger)


async def _process_goal_time_answer(level: str) -> None:
    wake_schedule.caffeinate_briefly()
    try:
        trigger = orchestrator.handle_goal_time_answer(_bridge, level)
    except Exception:
        logger.exception("Failed to process goal-time answer %r", level)
        return
    logger.info("Processed goal-time answer %r -> next trigger %s at %s", level, trigger.reason, trigger.at)
    await _reschedule_after(trigger)


async def _process_reminder_message(text: str) -> None:
    """Unlike every _process_* above, this is orthogonal to scheduling — no Trigger, no
    _reschedule_after. Awaited directly (not asyncio.create_task) by the poll loop rather
    than fired off in the background: a multi-turn conversation is order-sensitive (a
    second message must see the first one's saved pending_reminder_intake state), and
    Telegram messages arrive at human, not machine, pace, so serializing them is safe."""
    wake_schedule.caffeinate_briefly()
    try:
        await asyncio.to_thread(orchestrator.handle_reminder_message, _bridge, text)
    except Exception:
        logger.exception("Failed to process reminder intake message")


async def _process_reminder_confirmation(
    answer: str, title: str, notes: Optional[str], due_date_iso: Optional[str]
) -> None:
    wake_schedule.caffeinate_briefly()
    due_date = datetime.fromisoformat(due_date_iso) if due_date_iso else None
    try:
        await asyncio.to_thread(orchestrator.handle_reminder_confirmation, _bridge, answer, title, notes, due_date)
    except Exception:
        logger.exception("Failed to process reminder confirmation for %r", title)


async def _telegram_poll_loop() -> None:
    offset = None
    while True:
        try:
            updates = await asyncio.to_thread(telegram_notifier.get_updates, offset, 30)
        except Exception:
            logger.exception("Telegram getUpdates failed — retrying shortly")
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            callback_query = update.get("callback_query")
            if callback_query:
                await _handle_telegram_callback(callback_query)
            elif update.get("message"):
                await _handle_telegram_message(update["message"])


async def _handle_telegram_callback(callback_query: dict) -> None:
    telegram_notifier.answer_callback_query(callback_query["id"])  # stop the tap's loading spinner

    data = callback_query.get("data", "")
    if ":" not in data:
        logger.warning("Malformed Telegram callback_data: %r", data)
        return
    token, answer = data.split(":", 1)

    context = telegram_notifier.resolve_callback(token)
    if context is None:
        logger.warning("Telegram callback with unknown/expired token — ignoring")
        return

    if context["kind"] == "checkin":
        block_id = context["block_id"]
        if _is_duplicate(f"checkin:{block_id}"):
            logger.info("Ignoring duplicate check-in callback for %s", block_id)
            return
        asyncio.create_task(_process_checkin_answer(block_id, answer))
    elif context["kind"] == "day_boundary":
        event = context["event"]
        if _is_duplicate(f"dayboundary:{event}:{datetime.now().date()}"):
            logger.info("Ignoring duplicate day-boundary callback for %s", event)
            return
        asyncio.create_task(_process_day_boundary(event, answer))
    elif context["kind"] == "goal_time_budget":
        if _is_duplicate(f"goaltime:{datetime.now().date()}"):
            logger.info("Ignoring duplicate goal-time callback")
            return
        asyncio.create_task(_process_goal_time_answer(answer))
    elif context["kind"] == "reminder_confirm":
        if _is_duplicate(f"reminder_confirm:{token}"):
            logger.info("Ignoring duplicate reminder-confirmation callback")
            return
        asyncio.create_task(
            _process_reminder_confirmation(answer, context["title"], context.get("notes"), context.get("due_date"))
        )
    else:
        logger.warning("Unknown Telegram callback kind: %r", context.get("kind"))


async def _handle_telegram_message(message: dict) -> None:
    """Plain text messages (not button taps) — the entry point for conversational
    reminder intake. Every other kind of update (photos, stickers, voice — voice is
    deferred, see reminder_intake.py's module docstring) is ignored."""
    text = message.get("text")
    if not text or text.startswith("/"):
        return

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != str(telegram_notifier.TELEGRAM_CHAT_ID):
        logger.warning("Ignoring Telegram message from unexpected chat_id %r", chat_id)
        return

    if _is_duplicate(f"reminder_msg:{message.get('message_id')}"):
        logger.info("Ignoring duplicate Telegram message %s", message.get("message_id"))
        return

    await _process_reminder_message(text)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bridge, _telegram_poll_task
    _bridge = EventKitBridge()
    cal_granted, rem_granted = _bridge.request_access()
    if not (cal_granted and rem_granted):
        logger.error("Calendar/Reminders access not granted — nothing will work until it is.")

    if telegram_notifier.is_configured():
        _telegram_poll_task = asyncio.create_task(_telegram_poll_loop())
    else:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — "
                        "notifications will only be logged, not sent.")

    await _run_tick()  # RunAtLoad equivalent: run immediately rather than waiting
    yield
    if _current_task:
        _current_task.cancel()
    if _telegram_poll_task:
        _telegram_poll_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/status")
def status():
    state = load_state()
    return {
        "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
        "pending_followup_at": state.pending_followup_at.isoformat() if state.pending_followup_at else None,
        "pending_followup_reason": state.pending_followup_reason,
        "day_start_confirmed_date": str(state.day_start_confirmed_date) if state.day_start_confirmed_date else None,
        "day_end_confirmed_date": str(state.day_end_confirmed_date) if state.day_end_confirmed_date else None,
        "next_trigger_at": _next_trigger.at.isoformat() if _next_trigger else None,
        "next_trigger_reason": _next_trigger.reason if _next_trigger else None,
        "agent_paused": state.agent_paused,
    }


@app.get("/settings", response_class=HTMLResponse)
def get_settings(token: str = ""):
    """Opened from menu_bar.py's "Edit Settings…" item. Token travels as a query param
    (not a header/body) since it's a plain browser GET — the rendered form then carries
    it forward via a hidden input so the POST below is self-authenticating."""
    _check_token(token)
    raw = yaml.safe_load(orchestrator.RULES_PATH.read_text()) or {}
    return settings_page.render_settings_form(raw, token=token)


@app.post("/settings", response_class=HTMLResponse)
async def post_settings(request: Request):
    """Validates the submission through the exact same config._rules_from_dict every
    other rules.yaml read already trusts, BEFORE writing anything to disk — an invalid
    submission re-renders the form with the values the user typed and an error message,
    the real file untouched. A valid one writes via a temp-file-then-replace so a save
    interrupted partway through can never leave rules.yaml half-written."""
    form = await request.form()
    token = form.get("token", "")
    _check_token(token)

    new_raw = settings_page.parse_form_to_rules_dict(form)
    try:
        _rules_from_dict(new_raw)
    except ConfigError as e:
        return settings_page.render_settings_form(new_raw, token=token, error=str(e))

    merged_text = apply_rules_dict(orchestrator.RULES_PATH.read_text(), new_raw)
    tmp_path = orchestrator.RULES_PATH.with_suffix(".tmp")
    tmp_path.write_text(merged_text)
    tmp_path.replace(orchestrator.RULES_PATH)
    logger.info("Settings saved via /settings — %s updated", orchestrator.RULES_PATH)
    return settings_page.render_settings_form(new_raw, token=token, saved=True)


@app.post("/pause")
async def pause(request: Request):
    """Called by menu_bar.py's "Agent Active" toggle switching off. Cancels whatever
    tick is currently pending and persists the pause so it survives a server restart —
    see _run_tick's own guard for the restart case, and _reschedule_after's for anything
    that tries to reschedule while paused anyway (an incoming Telegram answer still gets
    processed for correctness, just never re-arms the loop)."""
    global _current_task
    body = await request.json()
    _check_token(body.get("token", ""))

    state = load_state()
    state.agent_paused = True
    save_state(state)
    if _current_task and not _current_task.done():
        _current_task.cancel()
        _current_task = None
    logger.info("Agent paused via /pause — no further ticks until /resume")
    return {"ok": True, "agent_paused": True}


@app.post("/resume")
async def resume(request: Request):
    """Called by menu_bar.py's "Agent Active" toggle switching on. Flips the persisted
    flag BEFORE calling _run_tick so that call's own agent_paused guard sees the new
    value and actually runs, recomputing (not guessing at) the correct next trigger."""
    body = await request.json()
    _check_token(body.get("token", ""))

    state = load_state()
    state.agent_paused = False
    save_state(state)
    logger.info("Agent resumed via /resume — running an immediate tick")
    await _run_tick()
    return {"ok": True, "agent_paused": False}


@app.post("/webhook/checkin")
async def webhook_checkin(request: Request):
    """Body: {token, block_id, answer}. block_id is any checkable block's own EventKit
    identifier (agent-authored task, FIXED commitment, or FLEXIBLE_SPECIFIC block) —
    whichever was named in the check-in notification. answer is one of 'completed' |
    'running_behind' | 'unexpected_plan'. Acks immediately and processes in the
    background — the actual replan involves two sequential Claude calls and can easily
    take 10-20+ seconds."""
    body = await request.json()
    _check_token(body.get("token", ""))
    block_id = body.get("block_id")
    answer = body.get("answer")
    if not block_id or answer not in orchestrator.ANSWER_TO_LOG_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"body must include block_id and answer in {list(orchestrator.ANSWER_TO_LOG_STATUS)}",
        )

    if _is_duplicate(f"checkin:{block_id}"):
        logger.info("Ignoring duplicate check-in for %s within dedup window", block_id)
        return {"ok": True, "duplicate": True}

    asyncio.create_task(_process_checkin_answer(block_id, answer))
    return {"ok": True, "accepted": True}


@app.post("/debug/send-test-checkin")
async def debug_send_test_checkin(request: Request):
    """Sends a real Telegram check-in notification FROM this server process, not the
    caller's — the button-tap token registered by telegram_notifier.send_checkin_notification
    only exists in whichever process's memory created it, so a standalone script calling
    that function directly can never have its buttons work, even with the server's poll
    loop running. Routing the send through the server itself (this endpoint) fixes that.
    Body: {token, block_id, title, text} — block_id defaults to a harmless placeholder,
    title/text default to a generic test message, if omitted."""
    body = await request.json()
    _check_token(body.get("token", ""))
    block_id = body.get("block_id", "test-block-id-not-real")
    title = body.get("title", "Test check-in")
    text = body.get("text", "Safe to tap any button.")
    telegram_notifier.send_checkin_notification(title, text, block_id)
    return {"ok": True}
