# Crono.Ai

An adaptive time-blocking agent for macOS. It reads generic "bucket" blocks on your
Calendar (e.g. "Work 9-12", "Hobby 6-8"), fills them in with specific tasks pulled from
Apple Reminders — or a long-term goal when there's spare time — and reshuffles the rest
of your day when something runs long or an unexpected plan comes up. It checks in with
you over Telegram at the start and end of each block, never more than that.

## Features

- **Classification** — every calendar event gets tagged each cycle as a real commitment
  (never touched), a generic bucket to fill, or an already-specific block to leave alone.
- **Day-start planning** — once you confirm the day's started, every open bucket gets
  filled with a real Reminder, sized to how long the task would realistically take — or a
  long-term goal from `~/goals/*.md`, only in a protected buffer.
- **Telegram check-ins** — a message with three buttons (done / still going / something
  came up) at the start and end of anything schedulable. No periodic pings otherwise.
- **Priority-aware reshuffling** — when something overruns into the next block, the
  agent judges relative priority from your own bucket descriptions and active goals, and
  decides whether to push the next block later or eat into its own time instead.
- **Conversational reminder intake** — text the Telegram bot what you want to be
  reminded of in your own words; it becomes a real Reminder, asking one clarifying
  question if the task itself is too vague.
- **Always running** — a persistent process computes the next moment it actually needs
  to do something and sleeps in between, so it costs nothing when there's nothing to do.
- **Menu bar app + web settings page** — a native menu bar app for Prevent Sleep / Agent
  Active toggles, and a local web form for editing config without hand-editing YAML.

## Requirements

- macOS (uses EventKit via `pyobjc` for Calendar/Reminders — no cross-platform path yet)
- Python 3.9+
- An [Anthropic API key](https://console.anthropic.com/) (Claude Haiku is used for all
  classification/planning calls)
- A Telegram bot (free) for notifications — `scripts/telegram_setup.py` walks through
  getting your `chat_id` once `TELEGRAM_BOT_TOKEN` is set

## Setup

These steps are the same regardless of how you run it day-to-day:

1. **Install dependencies**
   ```
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
2. **Create a `.env` file** with `ANTHROPIC_API_KEY`, `WEBHOOK_TOKEN` (any
   `secrets.token_urlsafe(32)` value), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. **Create a "Task Blocks" calendar** in Calendar.app — agent-created events live here,
   kept separate so they're unambiguous and get their own color.
4. **Copy the config templates and edit them for your own setup:**
   ```
   cp rules.example.yaml rules.yaml
   cp buckets.example.md buckets.md
   ```
   `rules.yaml` holds the numeric/text knobs (calendar names, sizing, timing) — see
   [docs/rules.md](docs/rules.md) for what each one actually does. `buckets.md` is a
   freeform description of your own buckets (e.g. "Work", "Hobby") — the model reads
   it fresh every cycle, so a renamed or new bucket is picked up without a code change.
5. **Grant Calendar/Reminders access** by running `scripts/check_access.py` from an
   actual **Terminal.app** window — not an IDE's integrated terminal, which can't
   trigger the real macOS permission prompts.

From here, pick one of the two ways to actually run it:

### Option A — Full experience (recommended)

Double-click **`Start Crono.Ai.command`** in the repo root. It starts the server and the
menu bar app together — no terminal typing needed after initial setup, and it no-ops if
either is already running.

The menu bar app gives you:
- **Prevent Sleep** — keeps the Mac awake so scheduled check-ins actually fire on time.
- **Agent Active** — an independent pause/resume switch for the agent itself.
- **Edit Settings…** — opens a local web form for editing `rules.yaml` through validated
  fields instead of hand-editing YAML.

Add it as a Login Item (System Settings → General → Login Items) for it to start
automatically, so the whole thing survives a reboot without you touching a terminal again.

### Option B — Manual / just the server

If you'd rather run it by hand or script around it yourself, skip the menu bar app
entirely:
```
.venv/bin/python -m uvicorn timeblock_agent.server:app --host 0.0.0.0 --port 8787
```
Also run from Terminal.app. You'll hand-edit `rules.yaml` directly instead of using the
web settings form, and you're responsible for keeping the Mac awake for scheduled
check-ins to fire (see [docs/rules.md](docs/rules.md) §8).

## Configuration

- [`rules.example.yaml`](rules.example.yaml) — copy to `rules.yaml`: calendars in scope,
  block sizing, timing floors, goal-fill weighting.
- [`buckets.example.md`](buckets.example.md) — copy to `buckets.md`: freeform
  descriptions of your own buckets and their relative priority.
- [`docs/rules.md`](docs/rules.md) — plain-language explanation of the actual scheduling
  logic, if you want to know *why* something happened on your calendar.
- [`docs/goals.md`](docs/goals.md) — how `~/goals/*.md` files get picked and scheduled.

## Project layout

```
timeblock_agent/
  eventkit_bridge.py     Calendar/Reminders read+write (pyobjc/EventKit)
  config.py               rules.yaml schema + loader + settings-page writer
  classify.py              FIXED / FLEXIBLE_BUCKET / FLEXIBLE_SPECIFIC classification
  day_layout.py            once-a-day full bucket-fill pass
  incremental_replan.py    mid-day replan given one block's resolution
  reminder_intake.py       conversational Telegram -> Reminders.app intake
  orchestrator.py          wires it all into the scheduler's actual behavior
  server.py                persistent FastAPI process (webhook + scheduler + poll loop)
  menu_bar.py              menu bar app: Prevent Sleep + Agent Active, Edit Settings
  settings_page.py         renders/parses the local web settings page
scripts/                  live-test and utility scripts (must run from Terminal.app)
fake_sandbox/             offline, assertion-based automated test suite — no Calendar/
                          Reminders access, no wall-clock waiting
docs/                     scheduling-logic and goals-mechanism reference docs
Start Crono.Ai.command     double-clickable launcher: server + menu bar app together
rules.yaml / buckets.md   your personal config (gitignored — copy from the .example files)
~/goals/*.md              your long-term goals (frontmatter + notes + append-only log)
~/timeblock-logs/         daily completion log, one file per day
```

## Testing

`fake_sandbox/` is a fully offline, assertion-based test suite covering the core
scheduling scenarios against an in-memory fake EventKit bridge:
```
.venv/bin/python fake_sandbox/run_all_tests.py
```
It makes real Claude API calls (~25-35 per full run). See
[fake_sandbox/EDGE_CASES.md](fake_sandbox/EDGE_CASES.md) for what's covered.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for what's left.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
