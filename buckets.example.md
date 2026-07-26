# Bucket definitions

Copy this file to `buckets.md` and edit it for your own setup:

```
cp buckets.example.md buckets.md
```

Edit `buckets.md` any time your buckets change — it's read fresh every classification and
replan cycle, so changes take effect on the very next tick, no restart needed.

This is **not** a list of trigger words. Describe what a bucket generally *means*, and
the model will recognize a new or renamed block in the same spirit, rather than needing
an exact title match — this matters because block names won't stay the same (e.g. once
school starts, "Work" might become "Project Time", "Classwork", "Revision", or something
else entirely).

## What generally makes something a "bucket" vs. already-specific

A **bucket** is an open-ended container with no specific task decided yet — it should get
filled with something concrete from Reminders, or a goal. A block is **already specific**
(not a bucket) if it names a concrete, self-contained activity that doesn't need anything
assigned to it — Gym, Lunch, Shower, a specific hobby session you've already decided on.

## My current buckets

<!-- Replace this with your own buckets. One example below to show the shape. -->

### Work
Anything related to my job, coursework, or freelance projects — whatever obligation-shaped
block shows up on the calendar, even if the literal word "work" isn't in the title.

Priority: **highest**. If a day's Work overruns into another bucket, Work should win —
shrink or delay the other bucket, not Work. Goal-time-eligible: **yes** — any spare time
this bucket has left over after real tasks is fair game to source a day's goal-time
request from (see "Goal Time" below and `docs/rules.md` §2).

### Goal Time
A synthetic bucket the agent creates itself when I answer the day-start "how much goal
time today?" prompt with Light/Balanced/Heavy. Sourced from Work's own leftover time
first (see `docs/rules.md` §2 for the exact order). Filled only with goal work from
`~/goals/*.md` — never a reminder, that's enforced in code, not just described here.
Same rough priority as whatever it was actually carved from that day — it can still give
up time later if something genuinely needs it, but shouldn't be the first thing
sacrificed either.

<!--
Add new buckets here as they show up on your calendar — a short paragraph on what the
bucket generally means, a priority note if it matters relative to your other buckets, and
whether it's goal-time-eligible (should it ever be raided to source a goal-time request?
default assumption is "yes" for anything obligation-shaped unless you say otherwise here
— e.g. a dedicated "Revision" block might be obligation-shaped but NOT something you want
goal time carved out of; just say so in its own description and it'll be respected). You
don't need to list every possible title it might have; describe the spirit and the model
will generalize.
-->
