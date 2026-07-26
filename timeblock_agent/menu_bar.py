"""macOS menu bar control + status app for the Crono.Ai server (see ROADMAP.md's "drop
passwordless-sudo requirement, add a menu bar control app"). Runs as its OWN separate
process, independent of the main FastAPI server (server.py) — a pure control/status
layer, never touches EventKit/Claude itself.

Why this exists instead of `pmset schedule wake` + passwordless sudo: the server's real
scheduling is already `asyncio.sleep()` inside the persistent process — `pmset` only
ever existed to guarantee the physical Mac wakes back up if the OS suspends it. On a
dedicated, wall-powered Mac mini, the power saved by letting the Mac actually sleep is
negligible, while a passwordless-sudo grant is a permanent, real security-relevant
system change. Preventing sleep entirely removes the need for either `pmset` or `sudo`.

Two independent toggles, both ON by default at launch:
  - "Prevent Sleep": holds a `caffeinate -s -i` subprocess alive for as long as it's
    checked. Entirely local to this process — no interaction with the main server.
  - "Agent Active": calls POST /pause / POST /resume on the running server
    (WEBHOOK_TOKEN-authenticated, the same `_check_token` pattern server.py already uses
    for every other POST endpoint) so the scheduler stops/starts ticking entirely.

Run with: .venv/bin/python -m timeblock_agent.menu_bar
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import httpx
import rumps
from dotenv import load_dotenv

load_dotenv()

from timeblock_agent.bucket_context import DEFAULT_BUCKETS_PATH  # noqa: E402
from timeblock_agent.goals import DEFAULT_GOALS_DIR  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("timeblock_agent.menu_bar")

# TIMEBLOCK_SERVER_URL lets this point at a non-default host/port (e.g. testing against
# the sandbox server, which also runs on 8787 — only one of the two can be up at once,
# same constraint TESTING.md already documents for the server itself).
SERVER_BASE_URL = os.environ.get("TIMEBLOCK_SERVER_URL", "http://localhost:8787")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN")
STATUS_POLL_SECONDS = 15

_MENUBAR_ICON = Path(__file__).parent / "assets" / "menubar_icon.png"


class CronoMenuBarApp(rumps.App):
    def __init__(self):
        # template=True: a black-glyph-on-transparent PNG that macOS recolors
        # automatically for light/dark menu bars, same convention every other menu bar
        # icon uses — falls back to the "Crono.Ai" text title if the asset is missing.
        icon_path = str(_MENUBAR_ICON) if _MENUBAR_ICON.exists() else None
        super().__init__("Crono.Ai", title=None if icon_path else "⏱", icon=icon_path, template=True)
        self.status_item = rumps.MenuItem("Status: —")  # no callback = disabled/non-clickable
        self.prevent_sleep_item = rumps.MenuItem("Prevent Sleep", callback=self.toggle_prevent_sleep)
        self.agent_active_item = rumps.MenuItem("Agent Active", callback=self.toggle_agent_active)
        self.settings_item = rumps.MenuItem("Edit Settings…", callback=self.open_settings)
        self.buckets_item = rumps.MenuItem("Edit buckets.md", callback=self.open_buckets)
        self.goals_item = rumps.MenuItem("Open Goals Folder", callback=self.open_goals)
        self.menu = [
            self.status_item, None,
            self.prevent_sleep_item, self.agent_active_item, None,
            self.settings_item, self.buckets_item, self.goals_item,
        ]

        self._caffeinate_proc: Optional[subprocess.Popen] = None

        # Both ON by default at launch, per design — "on unless turned off."
        self.prevent_sleep_item.state = True
        self._start_caffeinate()
        self.agent_active_item.state = True
        self._call_server("/resume")  # idempotent if already active; also un-pauses a prior session

        self.status_timer = rumps.Timer(self._poll_status, STATUS_POLL_SECONDS)
        self.status_timer.start()
        self._poll_status(None)

    # --- Prevent Sleep: entirely local, no server interaction ---

    def _start_caffeinate(self) -> None:
        if self._caffeinate_proc is not None and self._caffeinate_proc.poll() is None:
            return  # already running
        self._caffeinate_proc = subprocess.Popen(["caffeinate", "-s", "-i"])
        logger.info("caffeinate -s -i started (pid %d)", self._caffeinate_proc.pid)

    def _stop_caffeinate(self) -> None:
        if self._caffeinate_proc is not None and self._caffeinate_proc.poll() is None:
            self._caffeinate_proc.terminate()
            logger.info("caffeinate stopped")
        self._caffeinate_proc = None

    def toggle_prevent_sleep(self, sender: rumps.MenuItem) -> None:
        sender.state = not sender.state
        if sender.state:
            self._start_caffeinate()
        else:
            self._stop_caffeinate()

    # --- Agent Active: controls the running server's scheduler via /pause, /resume ---

    def toggle_agent_active(self, sender: rumps.MenuItem) -> None:
        new_state = not sender.state
        endpoint = "/resume" if new_state else "/pause"
        if self._call_server(endpoint):
            sender.state = new_state
        # else: leave sender.state as-is — the toggle didn't actually take effect
        # server-side, so the checkbox shouldn't claim it did.

    def _call_server(self, path: str) -> bool:
        if not WEBHOOK_TOKEN:
            logger.error("WEBHOOK_TOKEN not set in .env — cannot call %s", path)
            rumps.alert("Crono.Ai", "WEBHOOK_TOKEN isn't set in .env — can't control the server.")
            return False
        try:
            resp = httpx.post(f"{SERVER_BASE_URL}{path}", json={"token": WEBHOOK_TOKEN}, timeout=10)
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to call %s", path)
            rumps.alert("Crono.Ai", f"Couldn't reach the server at {SERVER_BASE_URL} — is it running?")
            return False

    # --- Settings / freeform docs: plain "open" shortcuts, no server round-trip needed
    # for the latter two (buckets.md / goals are files on disk, not server state) ---

    def open_settings(self, _sender: rumps.MenuItem) -> None:
        if not WEBHOOK_TOKEN:
            logger.error("WEBHOOK_TOKEN not set in .env — cannot open settings")
            rumps.alert("Crono.Ai", "WEBHOOK_TOKEN isn't set in .env — can't open settings.")
            return
        subprocess.run(["open", f"{SERVER_BASE_URL}/settings?token={WEBHOOK_TOKEN}"])

    def open_buckets(self, _sender: rumps.MenuItem) -> None:
        subprocess.run(["open", str(DEFAULT_BUCKETS_PATH)])

    def open_goals(self, _sender: rumps.MenuItem) -> None:
        subprocess.run(["open", str(DEFAULT_GOALS_DIR)])

    # --- Status polling ---

    def _poll_status(self, _sender) -> None:
        try:
            resp = httpx.get(f"{SERVER_BASE_URL}/status", timeout=5)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            self.status_item.title = "Status: server unreachable"
            return

        paused = data.get("agent_paused", False)
        self.agent_active_item.state = not paused
        if paused:
            self.status_item.title = "Status: paused"
            return

        reason = data.get("next_trigger_reason")
        at = data.get("next_trigger_at")
        if reason and at:
            # at is a full ISO datetime — just the HH:MM is what fits/matters in a menu.
            self.status_item.title = f"Next: {reason} at {at[11:16]}"
        else:
            self.status_item.title = "Status: idle"


def main():
    CronoMenuBarApp().run()


if __name__ == "__main__":
    main()
