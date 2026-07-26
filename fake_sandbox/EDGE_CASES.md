# Calendar use-case / edge-case coverage map

Maps every TESTING.md scenario and every concrete calendar edge case found while
building this automated suite to its actual test coverage — what's covered here
(assertion-based, via `fake_sandbox/`), what's only covered by the existing mocked
scratchpad regression suite (unit-level, different session, not in this repo), and what's
explicitly deferred with the reason why.

Tier legend (see `test_helpers.py`'s module docstring for the full rationale):
- **A** — pure logic, zero I/O, zero API calls, zero env setup.
- **B** — real Claude API call, but no bridge/env setup (calls `day_layout.plan_day` or
  `incremental_replan.replan_incremental` directly, bypassing `orchestrator.py`).
- **C** — needs the full env-var-before-import dance (imports `timeblock_agent.orchestrator`).

## TESTING.md scenarios

| # | Scenario | Tier | Test file | Notes |
|---|---|---|---|---|
| 1 | Classification sanity | B | `test_scenario1_classification.py` | Real-model classification correctness against `buckets.test.md`; cache mechanics (partial hits, invalidation) already covered by the mocked `test_classify_cache.py` — not duplicated here. |
| 2 | Day-start sizing + bucket extension | C | `test_scenario2_day_start_sizing.py` | Invariant-style assertions (not-squashed-or-unscheduled, no fixed-event overlap) rather than exact minute values, since real model sizing has some legitimate variance. |
| 3 | Goal-time sourcing: Hobby fallback + multi-goal split | C | `test_scenario3_goal_time_split.py` | Two separate cases (Hobby-fallback+split, protected-Buffer invariant) — an earlier combined version had Test Buffer accidentally become a second candidate donor bucket; splitting removed that self-inflicted confound. |
| 4 | Multi-day carryover pacing | C | `test_scenario4_multiday_pacing.py` | Single persisted `FakeEventKitBridge` reused across two synthetic "days"; `today_target_minutes` checked against the exact `_compute_task_pacing` formula, fed by whatever the model actually estimated on day 1. |
| 5 | Start/end check-ins fire at right scope | C | `test_scenario5_checkin_scope.py` | **Found and fixed a real bug while writing this test — see "Bugs found" below.** |
| 6 | "Completed" resolution logging | C | `test_scenario6_completed_logging.py` | **Found a real `completion_log.py` format limitation while writing this test (bug #26, now FIXED) — see "Bugs found" below.** Title now deliberately parenthesized (`"Test task beta (~20 min)"`) to serve as that bug's regression guard; retried up to 3x around a separate, still-OPEN finding (bug #27) surfaced while re-verifying the fix. |
| 7 | "Still going" — extension + priority cascade | C (full stack) + B (precision) | `test_scenario7_still_going_cascade.py`, `test_priority_cascade_end_to_end.py` | The Tier-C file drives the full `run_checkin_answer`→`apply_layout` stack across repeated rounds (identity persistence, bug #24 regression guard); the Tier-B file checks rule 4's two priority branches (PUSH/SHRINK-FROM-FRONT) with exact numbers via a single direct `replan_incremental` call. |
| 8 | Goal Time eaten first on overrun | C | `test_scenario8_goal_time_overrun.py` | Automated version of the manual live/fake-sandbox run from earlier this session; exact per-round shrink amounts asserted for both the bucket container and its goal-session task block (bugs #20/#22/#23). |
| 9 | Unexpected-plan cascade on an external block | C | `test_scenario9_unexpected_plan.py` | Calls `_resolve_external_block` directly — previously zero test coverage anywhere. 3 cases: cascade fires, no upcoming bucket (no cascade), `completed` (no followup). |
| 10 | Day-end confirmation | C | `test_scenario10_day_end.py` | 4 cases incl. evening-floor retry cadence — deliberately re-ticks at `now + 10s`, not the real retry interval, since `rules.test.yaml`'s near-midnight `evening_checkin_floor` (23:59) means the real retry interval always rolls into the next calendar day (a test-setup constraint, not a product issue). |
| 11 | Implicit day-boundary fallback | C | `test_scenario11_implicit_boundary.py` | Positive case (gap over threshold force-confirms) + negative control (gap under threshold does nothing) — proves the branch condition both fires and correctly doesn't. |
| 12 | Off-network Telegram delivery | — | — | **Not fake-sandbox-testable** — needs a real phone off real wifi/cellular. Stays a manual live-test item. |
| 13 | Weekly review | C | `test_scenario13_weekly_review.py` | Manual-edit detection spied on directly (a separate call after `run_weekly_review` returns would find nothing — its own internal snapshot purge already ran); interval-suppression re-tick confirmed via a second `run_scheduled_tick`. |

## `replan_incremental` resolution coverage

| Resolution | Tier | Test file | Notes |
|---|---|---|---|
| `running_behind` | B, C | `test_priority_cascade_end_to_end.py`, `test_scenario7_still_going_cascade.py`, `test_scenario8_goal_time_overrun.py` | Extensively covered — this was already the sole resolution exercised by the existing mocked suite too. |
| `unexpected_plan` | B, C | `test_replan_unexpected_plan_resolution.py`, `test_scenario9_unexpected_plan.py` | **Previously zero coverage anywhere in the codebase** (mocked or fake-sandbox) before this suite. The "cascade to a later bucket if the reopened bucket itself has no room" sub-branch is deliberately NOT asserted — harder to force deterministically than the core refill behavior, lower marginal value. |
| `completed` | A/free | `test_replan_unexpected_plan_resolution.py` (ValueError/empty-bucket cases only) | The interesting behavior for this resolution lives entirely in the caller (`_resolve_agent_task`/`_resolve_external_block`), not in `replan_incremental` itself — covered there instead (Scenario 6, 9's Case C, goal-credit edge case). |

## Concrete calendar edge cases

| Item | Tier | Test file::case | Notes |
|---|---|---|---|
| All-day FIXED event blocks everything that day | A | `test_calendar_edge_cases.py` Case 1 | No `is_all_day` special-casing anywhere in the codebase — relies entirely on the interval-overlap check, confirmed to work correctly by construction. |
| Zero-gap adjacency allowed (back-to-back, no buffer) | A | `test_calendar_edge_cases.py` Case 2 | Strict `<`/`>` comparison in `overlaps_fixed_event` — confirmed both at the helper level and through `validate_block` itself. |
| Exact `hard_cutoff` boundary (EXTEND and PUSH shapes) | A | `test_calendar_edge_cases.py` Case 3 | Boundary-inclusive (`new_end == cutoff` accepted, `cutoff + 1min` rejected) for both shapes. |
| Goal-time budget exceeds total active-goal capacity | A | `test_calendar_edge_cases.py` Case 4 | Documents current accepted behavior (leftover budget silently unspent once every active goal is capped) rather than "fixing" it — a real design choice, not a bug. |
| `is_implicit_day_boundary` exact-threshold math | A | `test_calendar_edge_cases.py` Case 5 | Strict `>` — gap exactly equal to the threshold does NOT count as an implicit boundary. |
| Rule 4 Branch B: next bucket LOWER priority -> SHRINK-FROM-FRONT | B | `test_priority_cascade_end_to_end.py` | Exact numbers, real model call, bounded 3-attempt retry. |
| Rule 4 Branch A: next bucket EQUAL/HIGHER priority -> PUSH | B | `test_priority_cascade_end_to_end.py` | Same file, buckets in reversed chronological order — no new shapes needed. |
| Goal-credit with no candidate available (`source=="goal"`, `goal_candidate is None`) | C | `test_goal_credit_edge_cases.py` | Locks in "silent no-credit, no crash" as intended behavior. |

## Bugs found while building this suite

- **Bug #25 (fixed):** `orchestrator._classify_today_events` queried `[now, day_end)`
  instead of midnight-to-day-end. Since `run_scheduled_tick` only ever sends a check-in
  notification for an external FIXED/FLEXIBLE_SPECIFIC block once it's already overdue
  (`e.end <= now`), `now` at the moment the user's ANSWER is processed
  (`run_checkin_answer`, real wall-clock time, no `now` passed by `server.py`) is always
  `>=` that block's own end — meaning the query would always exclude the very block
  being checked in on. A real, previously-undiscovered production bug: **every real
  check-in answer on a naturally-overdue FIXED/FLEXIBLE_SPECIFIC block would silently
  no-op.** Fixed to query from midnight, mirroring `_today_agent_blocks`'s existing
  pattern (the same bug class as bug #24, just in a different function). Found by
  `test_scenario5_checkin_scope.py`; that test is now bug #25's permanent regression guard.
- **Bug #26 (found, now FIXED):** `completion_log.py`'s log-line format
  (`- start-end task (bucket) — status [source]`) was genuinely ambiguous to parse back
  apart whenever the `task` title contained its own parentheses — confirmed directly
  (neither a greedy nor non-greedy regex group resolved both the "task has parens" and
  "bucket has parens" cases at once, and this sandbox's own `agent_calendar` name, `Task
  Blocks (Test)`, already has parens). A real, previously-latent risk for real reminder
  titles containing parentheses (e.g. "Renew passport (expires soon)") — would have
  misattributed `task`/`bucket` fields in `parse_log_line`, affecting weekly-review stats.
  Fixed by switching the whole line format to JSON-lines (`json.dumps`/`json.loads`
  escape whatever the fields contain, so the split is never ambiguous) — see ISSUES.md
  #26 for the full write-up. `test_scenario6_completed_logging.py`'s title reverted to a
  parenthesized one (`"Test task beta (~20 min)"`) to serve as this bug's own regression
  guard through the real orchestrator stack, no longer sidestepping it.
- **Bug #27 (found while re-verifying bug #26's fix, still OPEN):** re-running
  `test_scenario6_completed_logging.py` repeatedly to confirm the bug #26 fix surfaced a
  separate, pre-existing flake: `_resolve_agent_task` calls `replan_incremental`
  unconditionally regardless of answer, and the reminder just marked "completed" is
  still nominally "open" at that point (`_credit_reminder_completion`, which actually
  marks it done, runs afterward) — roughly 1 in 3 real Haiku calls proposed re-booking
  that same still-open reminder onto the SAME calendar event id just resolved. Since
  `FakeEventKitBridge.update_event` mutates the stored `CalendarEvent` object in place
  and `list_events` returns live references (not copies), this silently overwrote the
  very `triggering_task.start`/`.end` that `completion_log.log_block` reads immediately
  after — confirmed reproducing with both a paren-free and a parenthesized title, so
  unrelated to bug #26's own root cause. Not fixed in this pass (out of scope for the bug
  #26 fix); `test_scenario6_completed_logging.py` now retries (up to 3x, this suite's
  established convention for real-model non-determinism) around it so it stays bug #26's
  reliable regression guard without being blocked by this separate issue. See ISSUES.md
  #27 for the full write-up and candidate fixes.

## Deferred / out of scope

- Scenarios 1-8/13 were live-confirmed manually via the `fake_sandbox` CLI earlier this
  session before this automated suite existed; this suite's versions are new,
  independent, assertion-based coverage of the same ground, not a replay of those exact
  manual transcripts.
- Pure trigger-timing internals not directly exercised above (`compute_next_trigger`'s
  full candidate-selection logic beyond what Scenarios 10/11 touch, `set_followup`/
  `clear_followup`) are lower-value fake-sandbox targets — they're pure functions with no
  calendar/EventKit interaction at all, better suited to a lightweight mock-based test
  matching the existing scratchpad convention than to this bridge-driven suite.
