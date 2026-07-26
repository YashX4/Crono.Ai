#!/bin/zsh
# Double-click this file to start Crono.Ai: the server (if not already running) + the
# menu bar app together. Must be a real double-click in Finder (or opened via Terminal
# directly) — a .command file always runs inside a genuine Terminal.app process, which
# is what carries the entitlement to trigger the real Calendar/Reminders permission
# prompts (see BUILD_LOG.md) — the same reason every EventKit-touching script in this
# project has to run from Terminal.app specifically.
#
# PRODUCTION launcher — points at your real rules.yaml/calendars, not a sandbox. For
# sandbox testing instead, open a Terminal tab and `source scripts/sandbox_env.sh`
# before running the server/menu bar app manually (see TESTING.md).
#
# (A standalone double-click .app was tried first instead of this — it hit a real macOS
# wall: a brand-new unsigned app reaching into ~/Documents gets silently denied file
# access, no permission prompt ever shown, confirmed live. A .command file sidesteps
# this entirely since it runs inside Terminal.app, which already has the access this
# project has relied on all along.)
#
# This window auto-closes ~2 seconds after a CONFIRMED successful start (server
# responding + menu bar app actually running) — so this reads as "double-click, brief
# flash, done." If startup fails, the window stays open so you can see why.

# ${0:A:h} (zsh's symlink-resolving absolute-path modifier), not `dirname "$0"`: a plain
# `$0` is the path AS INVOKED, so launching this through a symlink (e.g. from
# ~/Applications, for Spotlight access) would `cd` into the symlink's own folder instead
# of the real repo — breaking every relative path below (.venv/bin/python, etc).
cd "${0:A:h}"

LOG_DIR="$HOME/Library/Logs/cronoai"
mkdir -p "$LOG_DIR"

if curl -s -o /dev/null --max-time 2 http://localhost:8787/status; then
  echo "Server already running on :8787 — leaving it alone."
else
  echo "Starting server (logging to $LOG_DIR/server.log)..."
  nohup .venv/bin/python -m uvicorn timeblock_agent.server:app --host 0.0.0.0 --port 8787 \
    > "$LOG_DIR/server.log" 2>&1 &
  disown
  sleep 3
fi

if pgrep -f "timeblock_agent.menu_bar" > /dev/null; then
  echo "Menu bar app already running — leaving it alone."
else
  echo "Starting menu bar app (logging to $LOG_DIR/menu_bar.log)..."
  nohup .venv/bin/python -m timeblock_agent.menu_bar > "$LOG_DIR/menu_bar.log" 2>&1 &
  disown
  sleep 1
fi

echo ""
# Poll instead of a single fixed-timeout check: the menu bar app's own startup calls
# POST /resume, which can trigger an immediate real tick (Claude + EventKit calls) that
# briefly blocks the server's single asyncio thread — a one-shot check can catch it mid-
# tick and misreport a healthy startup as failed.
started_ok=0
for _ in 1 2 3 4 5 6 7 8; do
  if curl -s -o /dev/null --max-time 2 http://localhost:8787/status && pgrep -f "timeblock_agent.menu_bar" > /dev/null; then
    started_ok=1
    break
  fi
  sleep 1
done

if [ "$started_ok" = "1" ]; then
  echo "Crono.Ai is running — look for the ⏱ icon in your menu bar."
  # Matched by window NAME, not tty: once this shell exits, Terminal reports `missing
  # value` for that window's tty property (confirmed live), so a tty-based match can
  # never find its own window after the fact — name stays stable either way.
  ( sleep 2; osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Crono.Ai.command")' ) &
  disown
else
  echo "Something didn't start correctly. Check the logs:"
  echo "  $LOG_DIR/server.log"
  echo "  $LOG_DIR/menu_bar.log"
  echo "This window will stay open (not auto-closing on failure)."
fi
