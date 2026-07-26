# Roadmap

What's left, roughly in the order it's worth doing.

## Deployment / verification

- **Install the menu bar app as a Login Item** and load
  `~/Library/LaunchAgents/com.cronoai.timeblockagent.plist` for true always-on operation,
  then verify auto-restart and reboot/login survival.
- **Off-network Telegram delivery** — needs a live check with a phone away from wifi;
  not automatable.
- **Set a spend cap/alert in the Anthropic Console** (recommended, not required).

## Features not yet built

- **"Hobby goal" tag** — let a goal file flagged as hobby-relevant fill a leisure bucket's
  own natural leftover time, independent of the daily goal-time budget.
- **Voice input for reminder intake** — a local Whisper/whisper.cpp transcription step
  feeding the same conversational-intake pipeline text messages already use.
- **Wake-from-sleep reconciliation** — trigger an immediate reconciliation tick on *any*
  Mac wake (not just a scheduled one), so a missed wake self-corrects faster. The implicit
  day-boundary fallback already covers this eventually; this would make it immediate.
  Needs a short design pass (likely an IOKit/NSWorkspace sleep/wake hook) before
  implementation.
- **Intelligent refill of a reopened bucket window** — when an in-progress agent task
  itself gets resolved as "something unexpected came up," the freed calendar time isn't
  automatically refilled with a different reminder yet. Low urgency — nothing is lost,
  it's just not immediately replaced.

## Contributing

Cross-platform support (the EventKit bridge is macOS-only by design) and additional
calendar/reminder backends are open design questions if anyone wants to take them on —
open an issue to discuss before sending a large PR.
