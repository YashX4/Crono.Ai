# Bucket definitions — SANDBOX (see TESTING.md)

Deterministic descriptions for the dummy sandbox calendar (`scripts/setup_test_sandbox.py`),
so classification/priority behavior is reproducible during testing rather than left to
judgment on obviously-fake titles. Not meant to be edited unless you change the sandbox
shape itself.

## What generally makes something a "bucket" vs. already-specific

A bucket is an open-ended container with no specific task decided yet — it should get
filled with something concrete from Reminders or a goal. A block is already-specific if
it names a concrete, self-contained activity needing nothing filled in.

## Current test buckets

### Test Work
An open-ended container — should be filled with a specific reminder. Highest priority of
the test buckets; if it overruns into Test Hobby, Test Hobby should give up time rather
than Test Work getting cut short. Goal-time-eligible: **yes** — a day's goal-time budget
should source from this bucket's own leftover slack first.

### Test Hobby
An open-ended container — should be filled with a specific reminder. Lower priority than
Test Work — expected to shrink (start pushed later, end unchanged), not just get delayed,
if Test Work overruns into it. Goal-time-eligible: **no** — only used as a goal-time
source when Test Work has no room left at all that day.

### Test Buffer
An open-ended container, but a protected buffer — never filled with a regular reminder,
only ever (optionally) with a goal's `next_action`, if one is available.

### Goal Time
A synthetic bucket created by the goal-time sourcing step (`orchestrator._source_goal_time`)
— sourced from Test Work's own leftover slack first, falling back to shrinking Test Hobby
only if Test Work has no room. Filled only with goal work, never a reminder (enforced in
code). Same rough priority as whatever it was actually carved from that day.

Not a bucket: Test Meeting (a real commitment — already forced `FIXED` by
`always_fixed_hints` matching "meeting") and Test Gym (already a concrete, specific
activity — moved/resized if needed, but never filled).
