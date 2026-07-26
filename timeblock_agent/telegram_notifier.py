"""Real notification delivery via Telegram (see spec: "Notification delivery").

Chosen over Pushcut for off-wifi reliability: the Mac reaches OUT to Telegram's servers
via long-polling (see server.py's telegram poll loop) rather than needing an inbound
connection reachable from wherever your phone happens to be — which is what made
Pushcut's model fundamentally dependent on being on the same wifi (or Tailscale). Free,
no account tier, and the callback direction works identically regardless of network.

One-time manual setup:
  1. Message @BotFather in Telegram, send /newbot, follow the prompts, get a bot token.
  2. Message your new bot anything (e.g. /start) so it has a chat to send to.
  3. Run scripts/telegram_setup.py to fetch your chat_id from that message.
  4. Put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

callback_data is capped at 64 bytes by Telegram, far too small for an EventKit identifier
(which alone is often 70+ characters) — so button taps carry a short single-use token
instead, resolved server-side via _pending (see _register_callback/resolve_callback).
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

_PENDING_MAX_AGE_SECONDS = 24 * 3600
_pending: dict[str, dict] = {}  # short token -> context, single-use


class TelegramError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _prune_pending() -> None:
    cutoff = time.time() - _PENDING_MAX_AGE_SECONDS
    for token in [t for t, ctx in _pending.items() if ctx["created_at"] < cutoff]:
        del _pending[token]


def _register_callback(context: dict) -> str:
    _prune_pending()
    token = secrets.token_hex(4)
    _pending[token] = {"created_at": time.time(), **context}
    return token


def resolve_callback(token: str) -> Optional[dict]:
    """Single-use: popped so a duplicate/replayed tap can't be processed twice."""
    return _pending.pop(token, None)


def _send(text: str, buttons: Optional[list[tuple[str, str]]] = None) -> None:
    if not is_configured():
        raise TelegramError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set in .env")

    body: dict = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if buttons:
        body["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
        }

    response = httpx.post(_api_url("sendMessage"), json=body, timeout=15)
    if response.status_code >= 300:
        raise TelegramError(f"Telegram API error {response.status_code}: {response.text}")


def send_checkin_notification(title: str, text: str, block_id: str) -> None:
    token = _register_callback({"kind": "checkin", "block_id": block_id})
    _send(
        f"{title}\n{text}",
        buttons=[
            ("Done", f"{token}:completed"),
            ("Still going", f"{token}:running_behind"),
            ("Unexpected", f"{token}:unexpected_plan"),
        ],
    )


def send_plain_notification(title: str, text: str) -> None:
    _send(f"{title}\n{text}")


def send_day_boundary_notification(kind: str, title: str, text: str) -> None:
    """kind: 'day_start' or 'day_end'."""
    token = _register_callback({"kind": "day_boundary", "event": kind})
    if kind == "day_start":
        buttons = [("Yes, I'm up", f"{token}:yes"), ("Not yet", f"{token}:no")]
    else:
        buttons = [("Yes, done", f"{token}:yes"), ("Still going", f"{token}:no")]
    _send(f"{title}\n{text}", buttons=buttons)


def send_goal_time_prompt(title: str, text: str) -> None:
    """Sent right after "yes, I'm up" — resolved via a "goal_time_budget" callback kind,
    handled the same way as checkin/day_boundary (see server.py)."""
    token = _register_callback({"kind": "goal_time_budget"})
    _send(
        f"{title}\n{text}",
        buttons=[
            ("None", f"{token}:none"),
            ("Light ~1h", f"{token}:light"),
            ("Balanced ~2-3h", f"{token}:balanced"),
            ("Heavy ~4h+", f"{token}:heavy"),
        ],
    )


def send_reminder_confirmation(
    title: str, notes: Optional[str], due_date: Optional[datetime], previous_token: Optional[str] = None
) -> str:
    """The confirmation step of conversational reminder intake (see reminder_intake.py) —
    shows exactly what was parsed and waits for an explicit tap before anything is written
    to Reminders.app. Resolved via a "reminder_confirm" callback kind (see server.py).

    The conversation can keep going past this point (the user pushing back revises the
    draft rather than starting a new one — see PendingReminderIntake), so each revision
    shows a FRESH confirmation with its own token; `previous_token`, if given, is popped
    first so a stale tap on the superseded buttons can't still create the old, wrong draft.
    Returns the new token so the caller can track it the same way for the next revision."""
    if previous_token:
        _pending.pop(previous_token, None)

    token = _register_callback(
        {
            "kind": "reminder_confirm",
            "title": title,
            "notes": notes,
            "due_date": due_date.isoformat() if due_date else None,
        }
    )
    body = f"Add this reminder?\n• {title}"
    if notes:
        body += f"\n• Notes: {notes}"
    if due_date:
        body += f"\n• Due: {due_date:%a %b %d, %H:%M}"
    _send(body, buttons=[("Yes, add it", f"{token}:yes"), ("Cancel", f"{token}:cancel")])
    return token


def invalidate_callback(token: Optional[str]) -> None:
    """Pops a token without resolving it — used when a pending reminder-intake thread ends
    (cancel, or a revision back to "clarify") while an earlier confirmation's buttons are
    still stale-outstanding, so a late tap on them can't act on out-of-date values."""
    if token:
        _pending.pop(token, None)


def answer_callback_query(callback_query_id: str) -> None:
    """Stops the loading spinner on the tapped button. Best-effort — never let a failure
    here break the actual answer processing."""
    try:
        httpx.post(_api_url("answerCallbackQuery"), json={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass


def get_updates(offset: Optional[int] = None, timeout: int = 30) -> list[dict]:
    """Long-polling fetch. offset = 1 + the highest update_id already processed. This is a
    blocking call for up to `timeout` seconds — callers must run it off the main event
    loop (see server.py's poll loop, which uses asyncio.to_thread — safe here since this
    is a plain HTTP call with no EventKit/pyobjc involvement)."""
    if not is_configured():
        raise TelegramError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set in .env")
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    response = httpx.get(_api_url("getUpdates"), params=params, timeout=timeout + 10)
    if response.status_code >= 300:
        raise TelegramError(f"Telegram getUpdates error {response.status_code}: {response.text}")
    return response.json().get("result", [])
