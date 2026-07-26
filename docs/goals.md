# How `~/goals/*.md` actually gets used

Plain-language explanation of what the code in `timeblock_agent/goals.py` and its callers
actually do with your goal files — as opposed to `templates/goal_template.md`, which is
just the file shape to copy. If you want to know why a particular goal did (or didn't)
show up on your calendar, this is the file to check.

## What a goal file is

Each `~/goals/*.md` file is frontmatter + free text:

```yaml
---
title: ...
priority: high | medium | low
status: active | paused | done
last_touched: YYYY-MM-DD
next_action: ...
---
## Notes
(free-form context, links, longer-term thinking)

## Log
<!-- Agent appends one line per goal-fill session below. Do not hand-edit above this line. -->
```

All five frontmatter fields are required — a file missing one, or with an invalid
`priority`/`last_touched`, gets skipped with a logged warning rather than crashing
anything else. `status: paused`/`done` goals are completely invisible to the rest of the
system: never picked, never shown as context, as if the file didn't exist.

## The weighted pick: `pick_goal()`

Whenever a single goal needs to be chosen, it's picked from whichever files have
`status: active`:

```
score = weight_staleness * (days_since_last_touched / most_stale_active_goal's_days)
      + weight_priority  * (priority_score: high=1.0, medium=0.6, low=0.3)
```

Weights are `rules.yaml`'s `goal_fill.weight_staleness`/`weight_priority` (0.6/0.4 by
default, should sum to 1.0). Staleness is normalized against whichever active goal has
gone longest untouched, so it's always relative to your current set of goals, not an
absolute day count. `pick_goal()` takes an optional `exclude` set of goal paths to skip —
this is what lets the same cycle pick a *second*, different goal once the first one is
already spoken for (see "More than one goal a day" below). Everything downstream only
ever sees a picked goal's `title` and `next_action`, never its `## Notes` body (a
deliberate cost guardrail: full goal content is never sent to Claude on a routine cycle).

## Where a goal-fill block can actually go

This is the part that trips people up: **a goal can only ever fill a protected buffer**,
never fill a normal bucket like Work or HobbyMaxxing directly — this is enforced in code
(`day_layout.validate_block`), not just prompt wording, after an early bug where the model
filled an entire real bucket with goal work instead of the reminders that belonged there.
(The reverse is also enforced: a protected buffer, including "Goal Time" below, can never
get a regular reminder either — `validate_block` rejects that too.)

Concretely, a goal-fill block only happens if:
1. It's a bucket matched by `rules.yaml`'s `protected_buffers` (substring match against
   the bucket's title or calendar name — currently `buffer`, `wind down`, `wind-down`,
   and `goal time`), **and**
2. That buffer's `allow_goal_fill` is `true`, **and**
3. A goal candidate actually got picked (no active goals → nothing happens, silently —
   the buffer is just left empty rather than forcing something in).

If it gets created, its title on the calendar is always the goal's own `next_action`
text — never something the model made up (`resolve_authoritative_title` enforces this
the same way it does for reminders).

This is also why, on an ordinary day with no goal-time request, goals barely show up at
all — a "buffer"/"wind-down" block is a fairly rare, specific thing to have on a
calendar. The **goal-time prompt** (the day-start "how much goal time today?" question —
see `rules.md` §2) exists specifically to fix that, described next.

## The goal-time prompt: a real, deliberate budget

Answering Light/Balanced/Heavy to the day-start goal-time question gives the system a
total-minutes budget for the day. `orchestrator._source_goal_time()` places that whole
budget as **one contiguous block sourced from exactly one bucket** — preferring a bucket
that reads as work/obligation-shaped (per `buckets.md`) and has enough of its own real
leftover slack (measured *after* reminders have already been placed that cycle, not
guessed beforehand) to cover the full request; falling back to shrinking a non-work
bucket (e.g. HobbyMaxxing) only if no single work-type bucket has enough room alone. See
`rules.md` §2 for the full sourcing order. The result is a synthetic **"Goal Time"**
bucket, which satisfies the `protected_buffers` rule above the same way any real
buffer would — nothing about `validate_block`'s gate changed to make this work.

## More than one goal in a day

If the day's goal-time budget is bigger than `rules.yaml`'s `goal_fill.max_minutes_per_goal`
(180 min / 3h by default), `allocate_goal_sessions()` splits it across multiple goals
rather than crediting one task with the whole thing: it repeatedly calls `pick_goal()`
excluding whatever's already been fully allocated, capping each pick at
`max_minutes_per_goal`, until the budget is spent or no active goals remain. The
resulting sessions get laid out as consecutive segments filling the "Goal Time" bucket's
window exactly — this part is plain Python arithmetic, not a model decision, since the
window and each session's length are already fully known by then.

The "Goal Time" bucket's own `notes` field carries a small marker
(`day_layout.encode_goal_time_meta`/`decode_goal_time_meta`) recording which real bucket
it was carved from. This is what lets an overrun on that donor bucket later reclaim Goal
Time's borrowed time first, unconditionally, before touching anything else — see
`rules.md` §5.

## The other way goals show up: as context, not content

Separately from goal-*filling*, every `status: active` goal's **title only** (not
`next_action`, not `## Notes`) gets passed as `active_goal_titles` into the incremental
replanner whenever it has to judge relative bucket priority — e.g. deciding whether an
overrunning Work block should eat into HobbyMaxxing's time or get pushed later instead
(see `rules.md` §5). This is read-only context for that judgment; it doesn't cause
anything to get scheduled by itself.

## Isolation for testing

`TIMEBLOCK_GOALS_DIR` (env var) redirects all of the above at a separate folder — the
sandbox test setup (`scripts/setup_test_sandbox.py`, see `TESTING.md`) points it at
`~/timeblock-test-goals/` with one dummy goal, so testing goal-fill/priority behavior
never touches your real goals directory.

## A gap worth knowing about

`goals.py` has a `record_completion(goal, entry)` function — it's meant to bump a goal's
`last_touched` to today and append a line to that goal's own `## Log` section after a
goal-fill block actually resolves, which is what would make the staleness-weighted
picking in `pick_goal()` actually rotate between goals over time. **Nothing in the
codebase calls it.** Right now, a goal-fill block resolving only writes to the shared
`~/timeblock-logs/YYYY-MM-DD.md` (via `completion_log.py`) — the goal's own file and
`last_touched` date never change unless you edit them by hand. Practically, this means
whichever active goal has the oldest `last_touched` will keep winning the weighted pick
indefinitely, even after repeated goal-fill sessions on it, until you manually update that
field. Worth fixing if goal rotation matters to you — happy to wire `record_completion()`
in wherever a goal-fill block gets resolved, just say the word.
