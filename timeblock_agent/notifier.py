"""Notification dispatch. Always logs (useful for testing without Telegram configured);
additionally sends a real Telegram message if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are
configured in .env. A delivery failure is logged, never raised — a notification not going
out shouldn't take down the scheduler tick.

kind:
  "checkin"            - the done/still-going/unexpected menu. Requires block_id.
  "day_boundary_start" - the "are you up?" yes/not-yet confirmation.
  "day_boundary_end"   - the "done for the day?" yes/still-going confirmation.
  "goal_time_prompt"   - the "how much goal time today?" none/light/balanced/heavy menu,
                         sent right after "yes, I'm up" and before the day gets planned.
  anything else        - plain FYI, no buttons.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from timeblock_agent import telegram_notifier

logger = logging.getLogger("timeblock_agent.notifier")


def send_notification(kind: str, title: str, text: str, block_id: Optional[str] = None) -> None:
    logger.info("[NOTIFY:%s] %s — %s%s", kind, title, text, f" (block_id={block_id})" if block_id else "")

    if not telegram_notifier.is_configured():
        return

    try:
        if kind == "checkin" and block_id:
            telegram_notifier.send_checkin_notification(title, text, block_id)
        elif kind in ("day_boundary_start", "day_boundary_end"):
            event = "day_start" if kind == "day_boundary_start" else "day_end"
            telegram_notifier.send_day_boundary_notification(event, title, text)
        elif kind == "goal_time_prompt":
            telegram_notifier.send_goal_time_prompt(title, text)
        else:
            telegram_notifier.send_plain_notification(title, text)
    except Exception:
        logger.exception("Failed to send Telegram notification")


def send_reminder_confirmation(
    title: str, notes: Optional[str], due_date: Optional[datetime], previous_token: Optional[str] = None
) -> Optional[str]:
    """The Yes/Cancel confirmation step of conversational reminder intake (see
    reminder_intake.py) — always logged, sent over Telegram only if configured, same
    graceful no-op-when-unconfigured behavior as send_notification above. Returns the new
    callback token (or None if unconfigured/failed) so the caller can track it — a later
    revision passes it back as `previous_token` to invalidate the now-stale buttons."""
    logger.info(
        "[NOTIFY:reminder_confirm] %s%s%s",
        title,
        f" (notes={notes})" if notes else "",
        f" (due={due_date.isoformat()})" if due_date else "",
    )

    if not telegram_notifier.is_configured():
        return None

    try:
        return telegram_notifier.send_reminder_confirmation(title, notes, due_date, previous_token=previous_token)
    except Exception:
        logger.exception("Failed to send Telegram reminder confirmation")
        return None


def invalidate_reminder_confirmation(token: Optional[str]) -> None:
    """Best-effort: pops a stale confirmation token (e.g. when a thread is cancelled or a
    revision returns to needing clarification) so a late tap on superseded buttons can't
    still act. Safe no-op if Telegram isn't configured or the token is already gone."""
    try:
        telegram_notifier.invalidate_callback(token)
    except Exception:
        logger.exception("Failed to invalidate a stale reminder-confirmation token")
