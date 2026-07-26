# How Crono.ai schedules your day, right now

This is a plain-language explanation of the actual decision logic — what the code does
and why — as opposed to `rules.yaml`, which is just the numeric/text knobs that logic
reads. If you want to change a threshold, edit `rules.yaml`. If you want to understand
*why* something happened on your calendar, this is the file to check first.

## 1. Classification — what kind of thing is this event?

Every event in your in-scope calendars gets re-tagged fresh, every single cycle (nothing
is cached or hardcoded), as one of three types:

- **`FIXED`** — a real commitment: a meeting, class, appointment. Never touched, never
  filled, never moved.
- **`FLEXIBLE_BUCKET`** — a generic container that should be filled with something
  specific (e.g. "Work", "HobbyMaxxing").
- **`FLEXIBLE_SPECIFIC`** — already names a concrete activity (e.g. "Gym", "Lunch").
  Movable/resizable if a reshuffle needs the room, but its content is never replaced.

`always_fixed_hints` in `rules.yaml` (currently `class`, `lecture`, `meeting`, `call`,
`appointment`) is a **hard rule, enforced in code** — if an event's title matches one of
these, it's forced `FIXED` before the model ever sees it. This exists because a bare
"Meeting" title with no attendees/location once got misclassified and filled with an
unrelated task.

The bucket-vs-specific call is **not** a keyword list — it's judged from `buckets.md`,
which you edit directly (see that file). Instead of matching exact words, the model reads
your own description of what a bucket generally means and your current buckets by name
and spirit, so a renamed or new block in the same spirit (e.g. "Work" becoming "Project
Time" once school starts) still gets recognized correctly without needing a config edit.
Ambiguous FIXED-vs-FLEXIBLE calls default to `FIXED`; ambiguous bucket-vs-specific calls
default to `FLEXIBLE_SPECIFIC` — in both cases, the safer wrong answer is "leave it
alone," not "overwrite it."

## 2. Day start — the goal-time question, then filling the buckets

Nothing gets filled until you confirm the day has started (see §7). On "yes, I'm up," a
second question goes out immediately after: **"How much goal time today?"** — None /
Light ~1h / Balanced ~2-3h / Heavy ~4h+. The day only actually gets planned once you
answer that. This number is a **budget for the whole day**, not a single block — see
below for how it actually gets spent.

### Filling buckets with reminders: realistic duration, not just "whatever fits"

A reminder no longer just gets sized to fit whatever room happens to be left in its
bucket. Claude judges how long the task would **realistically** take, and:

- If it fits the bucket alongside other reminders, it's scheduled at its real estimated
  length — leftover room in the bucket is real leftover room, not silently absorbed by
  stretching a task to fill it.
- If a task realistically needs more time than fits today, and another instance of a
  similarly-shaped bucket exists later *today* with room, it splits across both (same
  reminder, two blocks) rather than being truncated. If a bucket's whole realistic
  reminder workload doesn't fit its own window at all, the bucket's own end can get
  pushed later to fit (never past a `FIXED` event or the evening floor) — day-start
  gained this capability alongside the rest of this rework; previously only a mid-day
  replan could resize a bucket.
- If it still doesn't fit anywhere today, see "Multi-day tasks & due dates" below — a
  reminder with a real deadline gets tracked and paced across days; one without any
  deadline signal just falls into `unscheduled_reminder_ids`, reconsidered fresh
  tomorrow, exactly as it always has.
- Preference, not a hard rule: avoid stacking many different task types back-to-back in
  one bucket window (e.g. reminder, reminder, then a goal task right after) — spread
  things out a little where there's a real choice. Fine to do it anyway if there's
  genuinely no better option; this is about avoiding unnecessary fragmentation, not a
  constraint that should ever leave time unused.

### Multi-day tasks & due dates

A reminder that's genuinely too big for one sitting gets paced across days if it has a
deadline — its own `due_date` field, or a date recognized in its title/notes text (e.g.
"Essay - due Friday"). No deadline signal at all → no tracking, same as always
(`unscheduled_reminder_ids`, reconsidered fresh tomorrow with no memory of size).

**Day 1 vs. every day after.** A task's *total* realistic size only exists once Claude
has estimated it — that's an output of the day-start pass, not something known in
advance. So the very first day a big task is seen, it's handled exactly like the
single-day case above (best-effort fit, overflow to `unscheduled_reminder_ids`) — but
that day's estimate gets persisted (`task_progress.py`, keyed by reminder, survives
across days on purpose). From the *second* day onward, that persisted total (minus
whatever's actually been completed via a "done" check-in — see below) is what pacing
works from.

**How much to schedule today.** Not just an even split of remaining time over remaining
days — the system looks ahead at how much real `FLEXIBLE_BUCKET` time actually exists
between today and the deadline (one extra classification call, only made at all if some
reminder actually has a deadline — zero cost on an ordinary day). If that real capacity
falls short of what's needed, the task is flagged **at risk** and today's target gets
front-loaded higher than an even split, rather than deferring the problem until it's too
late to fix. A deadline that's already passed gets the maximum push: everything
remaining, today. This is a deliberate approximation, not a perfect forecast — it doesn't
know what else might get scheduled on a future day, or net out a future meeting sitting
inside a future bucket — but it's re-derived fresh every single day, so it self-corrects
as things change rather than committing to a stale plan.

**Getting credit.** Answering "done" at a check-in only credits that session's actual
scheduled duration toward the task's total — not the whole remaining amount — since
"still going"/"unexpected" don't represent finished work. Once accumulated credit meets
the total estimate, the Reminder itself gets checked off in Reminders.app automatically
(see "A gap this also fixes" below) and the tracking entry is dropped; otherwise it
persists, updated, for tomorrow's pacing.

**A gap this also fixes:** completing a check-in never used to mark the underlying
Reminder done at all — `bridge.complete_reminder()` existed but nothing ever called it,
for *any* reminder, multi-day or not. Fixed as part of this work: an ordinary single-day
reminder now gets completed the moment its check-in resolves "done," same as a multi-day
one does once its full credit is met.

**If it's at risk of missing its deadline,** you'll see a note folded into that day's
existing "Day started" notification (no new notification kind, per this project's stance
against extra alerts) — not a separate ping, and not repeated beyond that once-a-day
mention.

### Where the day's goal-time budget actually comes from

Once you've stated a budget (anything but "None"), the whole thing is placed as **one
single contiguous block**, sourced from exactly one bucket — never stitched together from
several different times of day:

1. **Check work-type buckets first** (per `buckets.md` — not a hardcoded name; once
   school starts this could be "Classwork," "Project Time," etc., and a bucket can be
   marked in `buckets.md` as never eligible to give up time this way at all — e.g. a
   dedicated "Revision" block for exam prep, even though it's obligation-shaped). Among
   the eligible ones, if any single bucket's own **natural, already-unused slack** (after
   reminders have filled what they realistically need) is enough to cover the *entire*
   budget on its own, place the goal-time block there — no boundary changes at all, it's
   just using room that was already going spare. If more than one qualifies this way,
   prefer whichever has the most slack.
2. **If no single work-type bucket's natural slack alone covers the whole budget**, the
   entire budget instead comes from shrinking a lower-priority bucket (e.g. HobbyMaxxing)
   — its own **end moves earlier** while its start stays put, and Goal Time occupies
   whatever tail got freed up. (This is the mirror image of the overrun-cascade mechanic
   in §5, which eats a bucket's *start* instead — different situation: a cascade is
   displaced forward by whatever just overran into it, while this carve-out is choosing
   where inside an untouched bucket to place Goal Time, and keeping the bucket's own
   front intact protects whatever real task might already be placed there, since
   `plan_day` always fills a bucket from its earliest room first.) This is the one case
   goal time is allowed to touch Hobby — and it always happens as one clean block there
   too, not a blend of a little bit from Work and a little more from Hobby. If the chosen
   bucket doesn't actually have enough of its own total time to cover the full request
   (rare — needs a bucket smaller than the ask), it sources as much as it safely can
   instead of failing outright, and you'll see this called out in the "Day started"
   notification ("Only found N of M min of goal time today") rather than being silently
   under-delivered.
3. **If there's no work-type bucket at all that day**, the whole budget is sourced from
   Hobby (or whatever exists) directly, same shrink mechanic, same single-block result.
4. **Once the day's budget is placed, stop.** Any other leftover, reminder-less time
   elsewhere (e.g. Hobby's own natural gaps, on a day the budget was satisfied entirely
   from Work) stays empty — answering "Light" doesn't mean "top up every gap in the day
   with goal work," it caps the total. (A future, not-yet-built idea: a goal file
   explicitly tagged as a "hobby goal" could be allowed to fill Hobby's own natural
   leftover time independent of this budget — not built, flagged for later.)

If the budget is **None**, none of the above runs — reminders fill what they can,
anything left over anywhere just stays empty, exactly like a normal bucket with no
reminders left to place.

Like every other block placement in this system, Goal Time is checked in code against
real `FIXED` events (meetings, etc.) before it's ever created — if the computed window
would land on one, goal time is skipped for the day entirely rather than risk double-
booking a real commitment. Goal time is a nice-to-have; a real commitment never is.

### More than one goal in a day

`pick_goal()`'s existing weighted score (staleness × priority, see `goals.md`) still picks
which goal gets worked on — but no single goal should be credited more than **~3 hours of
goal time in a day, total** (not per sitting — if a goal already has 3h from earlier that
day, it's excluded from being picked again regardless of how the remaining time is
sourced). If the day's total budget is bigger than that (e.g. Heavy, ~4h), once a goal
hits its cap, re-run the scoring — excluding whatever's already fully allocated today — to
pick the next goal for the remaining time, rather than dumping the whole budget onto one
task. Since the whole budget is one contiguous block (see above), splitting across
multiple goals means splitting that one block into consecutive same-bucket segments, one
goal per segment — not multiple separate blocks scattered through the day.

### Filling mechanics that don't change

- **Protected buffers** (`rules.yaml`'s `protected_buffers` — `buffer`, `wind down`,
  `wind-down`, `goal time`) are still never filled with a regular reminder, only ever
  (optionally) a goal — this is the same mechanism the "Goal Time"/work-slack spillover
  blocks ride on, nothing new was added to that gate itself. Both directions of this rule
  are hard code-level checks in `validate_block` (a goal block can't land *outside* a
  protected buffer, and a reminder can't land *inside* one), not just prompt wording.
- The block's displayed title is always pulled from the real reminder/goal data, never
  the model's own freeform wording (`resolve_authoritative_title`).
- Agent-created blocks always live in the dedicated `agent_calendar` ("Task Blocks");
  bucket *containers* themselves (including any synthetic "Goal Time" bucket) live in your
  own real calendars.

### Resolved decisions (for the record)

- The multi-day/due-date carryover system above is **explicitly deferred as its own
  follow-up project** — not part of this rework.
- "Avoid stacking task types back-to-back" only applies within one bucket's own window,
  not across bucket boundaries — a reminder-filled Work bucket ending right where a
  separate Goal Time bucket begins is completely normal.
- The day's goal-time budget is always placed as a **single contiguous block** sourced
  from exactly one bucket (see above) — never blended from two different times of day.

## 3. What actually gets a check-in notification

You get a Telegram check-in at the **start and end** of:
- an agent-authored task block (whatever's filling a bucket)
- any `FIXED` event (a real meeting, class, etc.)
- any `FLEXIBLE_SPECIFIC` block (gym, lunch, etc.)

You do **not** get checked in on a bucket *container* itself (e.g. "Work" as a whole) —
only on whatever specific thing is inside it. There is no periodic/hourly "how's it
going" ping anymore — if nothing is starting or ending, nothing fires.

## 4. Resolving a check-in

Three possible answers, each doing something different:

- **Completed** — logged to `~/timeblock-logs/YYYY-MM-DD.md` with the block's own
  scheduled start/end, marked resolved (never re-asked about again), no replanning.
- **Still going / running behind** — the current block's end gets extended. The task/
  reminder filling it is never silently swapped for a different one mid-extension — the
  system checks that the block returned still matches what was actually being asked
  about. If the new end would overlap the *next* bucket, see §5.
- **Something unexpected came up** — similar to "running behind," but treated as a
  genuine plan change rather than just "taking longer." For a block with no agent
  metadata (a FIXED event or FLEXIBLE_SPECIFIC block), this cascades as a displacement
  signal onto whichever bucket comes next chronologically.

In both of the last two cases, the reshuffle never lets a proposed change overlap a real
`FIXED` event — that's a hard rejection check, independent of what the model proposes.

## 5. Priority-aware cascading — "something has to give"

When an overrun would eat into the next bucket's start time, the system has to decide who
loses time. **Priority is judged dynamically, from context, every time this comes up** —
there's no static "Work = high, HobbyMaxxing = low" field in `rules.yaml`. The model
weighs the two buckets' titles against each other, using your active goals
(`~/goals/*.md`, whichever are `status: active`) and — most heavily — whatever
`buckets.md` says about each bucket's priority, if it says anything specific.

- If the next bucket is **equal or higher priority**: it gets pushed later, start and end
  both shifting by the same amount — it keeps its full original duration, just later.
- If the next bucket is **lower priority**: it gets eaten into instead — its **start**
  moves later to wherever the overrun now ends, but its **end stays where it was**. It
  loses real time today rather than just getting delayed. This is deliberate: the
  intent (from real usage) is that on a day where Work runs long, HobbyMaxxing should
  lose today's time rather than push everything later or leave Work unfinished.

Because this is inferred per-cycle rather than configured, its accuracy is bounded by how
well `buckets.md`/active goals communicate real priority — writing an explicit priority
note in `buckets.md` (as the current one does for Work vs. HobbyMaxxing) closes most of
that gap; bare title-guessing is only the fallback for a bucket you haven't described yet.
The weekly review (see §9) narrows this further over time by turning how you've actually
been manually correcting the schedule into observations in `preferences.md`, which every
classification/planning/replan call now reads alongside `buckets.md`.

**A synthetic "Goal Time" bucket (see §2) participates in this same cascade, in both
directions:**
- If the *Work* task itself overruns past Work's own end, and Goal Time sits adjacent to
  it (because that's where its time was borrowed from), the overrun eats into Goal Time
  **first** — before it would ever reach anything else. It's really Work's own budget on
  loan, so Work reclaims it before touching anyone else's time.
- If the *Goal Time* task itself overruns, it cascades using the normal priority judgment
  above like any other bucket would — eating into whatever's lower-priority and
  chronologically next (could be Hobby).

## 6. Day boundaries — knowing when the day starts and ends

Two independent ways this gets decided:

1. **Floor confirmation** — once `morning_checkin_floor`/`evening_checkin_floor` time
   has passed (09:00 / 22:00 by default) without that day being confirmed yet, you get an
   "are you up?" / "done for the day?" Telegram prompt. If you don't answer, it re-asks
   every `floor_confirmation_retry_minutes` (30 min by default) rather than nagging
   continuously or giving up silently.
2. **Implicit gap fallback** — if `day_boundary_gap_hours` (4h by default) passes with
   no run of the scheduler at all (dead phone, Mac never woke), the next run treats it as
   an unconfirmed day boundary regardless of what time it actually is.

Nothing about the rest of the system (filling buckets, checking in) runs until day-start
is confirmed for today.

## 7. The trigger scheduler — deciding when to wake up next

After every action, the scheduler computes the single next moment it actually needs to do
something — the earliest of: the next checkable block's start, its end, a pending
follow-up delay (from "still going"/"unexpected plan," `followup_delay_*_minutes`), or a
day-boundary floor/retry. There's no generic polling cadence layered on top of this.
`safety_backstop_minutes` (60 min) is not something you'll ever see as a notification —
it's a purely internal fallback so the process always has *something* to wake up for,
even if every other candidate is somehow exhausted.

## 8. Sleep/wake

The scheduler's real timer is `asyncio.sleep()` inside the persistent process itself —
it computes the next trigger and just waits for it. The Mac isn't expected to actually
system-sleep in between: `timeblock_agent/menu_bar.py`'s "Prevent Sleep" toggle (on by
default) holds a `caffeinate -s` assertion for as long as it's checked, which is what
keeps the process (and its asyncio timer) alive and ticking on schedule without needing
anything OS-level to wake it back up.

`wake_schedule.py`'s `pmset schedule wake` code still exists as an unused-by-default
fallback (still no-ops gracefully without passwordless `sudo` configured — see
`BUILD_LOG.md`), but it's no longer the primary mechanism: on the dedicated, wall-powered
Mac mini this runs on, the power saved by letting the Mac actually sleep is negligible,
while granting passwordless `sudo` is a real, permanent security-relevant system change.
`pmset` wake-scheduling also has a documented history of occasionally missing wakes on
macOS — part of why §6's implicit-day-boundary fallback exists as a safety net regardless
of which wake mechanism is in play. A brief separate `caffeinate` assertion (90s, spawned
by the server itself, not the menu bar app) still wraps each actual tick too, so a real
EventKit/Claude call in progress can't get interrupted even if "Prevent Sleep" happens to
be off.

The menu bar app's second toggle, "Agent Active," is a fully independent on/off for the
agent itself (via `POST /pause`/`POST /resume` on the running server) — unlike "Prevent
Sleep," which only affects whether the Mac can sleep, "Agent Active: off" means no
check-ins, no day-start, no scheduling at all, until switched back on. This state is
persisted (`AgentState.agent_paused`), so it survives a server restart rather than
silently resuming.

## 9. Weekly review — learning `preferences.md` from how the week actually went

Once every `weekly_review_interval_days` (7 by default — `rules.yaml`), a review pass
runs on whatever tick happens to notice it's due (checked directly against
`state.last_weekly_review_at` + `now`, same as the morning/evening floor checks — not a
separate notification-driven flow). If it's overdue when a tick notices, it retries every
`weekly_review_retry_minutes` (60 min by default) rather than either nagging or silently
skipping a whole week. The very first review isn't due until `weekly_review_interval_days`
after your *first ever confirmed day-start* — not from install time — so a fresh install
with zero history doesn't immediately queue a review with nothing to say.

**What it looks at, over the window since the last review:**

- **Completion-log patterns** (`~/timeblock-logs/*.md`) — per-bucket counts of
  completed / bumped / still-in-progress, the same statuses every check-in already logs.
- **Silent manual calendar edits** — every agent-authored task block gets a snapshot
  recorded (`block_snapshots.py`) at the moment it's created or updated, holding "exactly
  what the agent last wrote." At review time, the current agent-calendar events in that
  window are compared back against their snapshots: retitled, resized, or genuinely
  deleted outside the normal check-in flow all count as a manual correction worth
  learning from. A block the system itself deleted as part of a normal reshuffle never
  shows up here — its snapshot is removed at the same moment, not left behind to look like
  a manual edit.

One Claude call (only made at all if there's something to report — an empty week costs
nothing) turns those stats + diffs into 2-5 plain-language sentences, appended under
`preferences.md`'s `## Observations` heading — never rewriting anything above that line,
which is yours to edit directly, mirroring how `goals.md` splits frontmatter from its own
append-only log. `preferences.md` is folded into the same freeform context string
`buckets.md` already provides to classification/day-layout/incremental-replan, so this
needs no new prompt plumbing anywhere.

Once the review completes (whether or not it had anything to say), you get one brief FYI
notification ("Weekly review done — N new observations") — no buttons, nothing to answer
— and any snapshot whose own block is from a date before today gets purged, so the store
doesn't grow forever. Purging is deliberately keyed on the *block's own date*, never on
when the review itself ran: a block created the same day a review happens to fire must
still survive to give a same-day manual edit a fair chance to be caught by whatever
review comes after (an earlier version purged by review timing instead, which could
silently make a same-day edit undetectable forever — fixed after catching it live). A
failed review (an EventKit/Claude error mid-pass) leaves `last_weekly_review_at` untouched
so the retry cadence above picks it back up soon, rather than silently losing that week's
observations.

## 10. Conversational reminder intake via Telegram

Direct-to-Reminders.app editing stays fully supported and unchanged — this is an
additional path, not a replacement. The idea: text the Telegram bot what you want to be
reminded of, in your own words, and it becomes a real reminder — no need to phrase it
like a formal task title.

**How it works:**

1. You send the bot a plain text message (voice is a planned fast-follow — see below —
   not part of this first version).
2. One Claude call (`reminder_intake.py`) decides: is the task itself clear enough to act
   on? If yes, it extracts a title (rephrased as an imperative, "Call the dentist" not "I
   need to call the dentist"), optional notes, and a due date **only if you actually
   stated one** (resolving something like "Friday" against today's real date) — no due
   date is left as no due date, exactly like a reminder you'd type directly; it's never
   interrogated into existence.
3. If the task itself is too vague to act on at all ("remind me to do that thing" — no
   concrete action named), the bot asks **one** short clarifying question back and waits
   for your reply, continuing the same conversation thread until it's clear enough (or
   you cancel by saying so in plain language — "never mind," "forget it," etc.). It never
   asks a clarifying question just because a due date wasn't mentioned — only when the
   task content itself is unclear.
4. Once resolved, you get a confirmation message showing exactly what it parsed (title /
   notes / due date) with **Yes, add it** / **Cancel** buttons — nothing is ever written
   to Reminders.app without that explicit tap, the same "safer to confirm than to guess"
   posture this whole system already takes everywhere else.
5. Once created, it's a completely normal reminder in your existing Reminders list —
   indistinguishable from one you typed by hand, picked up by the same day-start/pacing
   machinery as anything else.

A pending clarification conversation expires after `reminder_intake_timeout_minutes` (if
you never reply) so a random later text message can't get misread as continuing a stale
thread from hours ago.

**Voice input** (a real, wanted feature — deferred, not scoped out): since Claude doesn't
accept raw audio, a voice message needs a speech-to-text step first. Decided: local
Whisper (whisper.cpp / faster-whisper) rather than a cloud STT API, to avoid adding a
second AI provider and any per-use cost — voice would just become an alternate way to
produce the same text input, transcribed then fed through the exact same pipeline above,
not a separate code path.
