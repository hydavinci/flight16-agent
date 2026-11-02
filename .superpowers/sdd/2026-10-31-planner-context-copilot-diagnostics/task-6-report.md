# Stage 5 Task 6 Report

## Status

Implemented against completed Stages 2-4 at base `b9e6541`.

## Binding adaptations and original-plan deviations

- Persisted `Conversation.planning_telemetry` through the existing SQLite
  `conversations.state_json` codec. No table or migration was added.
- Kept planner observations call-local while planning runs without the
  conversation lock. Telemetry is attached only to the freshly reloaded record
  after the existing exact reservation checks succeed.
- Failure telemetry is attached only in the reservation-validated failure save.
  No stale pre-call conversation is saved from `finally`.
- Preserved the typed `PlanningFailure.cause`. Managed `CopilotError` failures
  are handled in `ConversationManager._run()`, not `_fail()`.
- Used `PlanningScheduler` and `ExecutionTurnScheduler` through the real managed
  `ConversationManager.submit()` path.
- Preserved the current `conservative_failure_transition` result rather than
  restoring obsolete follow-up behavior.
- Cleared prior telemetry in the same `queue_message()` save that admits the
  next message to `queued`.
- Used the existing authenticated `Conversation` response; no duplicate API
  schema, endpoint, frontend, CI, dependency, or Stage 6 changes were added.

## Files

- `src/flight_agent/verification/contracts.py`
  - Added optional `planning_telemetry`.
- `src/flight_agent/verification/sqlite_codec.py`
  - Added telemetry to explicit `state_json` encoding; missing legacy values
    continue to validate as `None`.
- `src/flight_agent/verification/coordinator.py`
  - Preserved `PlanningFailure.cause`, collected call-local observations,
    cleared telemetry on admission, and published telemetry only after exact
    reservation revalidation in success/failure saves.
- `src/flight_agent/verification/routes.py`
  - Added allowlisted managed Copilot logging with `exc_info=False`.
- `tests/test_planning_telemetry.py`
  - Added managed success/failure/supersession, queue clearing, SQLite
    restart/export/legacy decode, API exposure, and privacy coverage.

## RED

Command:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest tests/test_planning_telemetry.py -q --tb=short
```

Output:

```text
FFFFFFFFFF                                                               [100%]
10 failed in 5.75s
```

Failures showed the missing `Conversation.planning_telemetry`, missing planner
`observe` integration, missing managed safe-log branch, and resulting stale
telemetry behavior.

Typed-cause diagnostic RED:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest \
'tests/test_planning_telemetry.py::test_managed_provider_failure_persists_only_safe_diagnostics_and_logs[authentication]' \
-q --tb=short
```

```text
F                                                                        [100%]
E   AssertionError: PlanningTelemetry(... failure=None) != PlanningTelemetry(... failure=CopilotDiagnostics(...))
1 failed in 0.44s
```

## GREEN

Focused telemetry:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest tests/test_planning_telemetry.py -q
```

```text
...........                                                              [100%]
11 passed in 5.72s
```

Focused scheduling, recovery, SQLite, route, history, and Copilot regressions:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest \
tests/test_planning_telemetry.py tests/test_scheduler_runtime.py \
tests/test_scheduler_web.py tests/test_scheduler_recovery.py \
tests/test_sqlite_store.py tests/test_agent_web.py tests/test_agent_history.py \
tests/test_agent_recovery.py tests/test_copilot.py -q
```

```text
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 92%]
.................                                                        [100%]
233 passed in 28.89s
```

Static checks:

```text
ruff check <four amended source files> tests/test_planning_telemetry.py
ruff format --check tests/test_planning_telemetry.py
git diff --check
```

```text
All checks passed!
1 file already formatted
```

All four amended source files were already reported unformatted by Ruff at
base `b9e6541`; they were not wholesale reformatted to avoid unrelated churn.

## Evidence

### SQLite restart/export

`test_sqlite_restart_and_export_preserve_allowlisted_telemetry` creates a real
SQLite store, saves telemetry, constructs a new store instance, and verifies
both restarted state and exported conversation JSON. A separate regression
removes the optional key from `state_json` and verifies restart decodes it as
`None`.

### Reservation supersession

`test_superseded_managed_success_does_not_publish_stale_telemetry` schedules
managed planning, publishes a newer record while the planner is unlocked, then
releases the stale planner. The newer status, pending decision, and telemetry
remain unchanged and no stale answer is appended.

### Logging/privacy

The managed failure test covers all six Copilot categories. Durable state and
captured logs exclude provider stdout/stderr, raw request ID, credentials,
paths, chained exception text, and traceback. The single provider log contains
only conversation ID, reservation state, category, and exit code, with no
`exc_info`.

## Self-review

- **Concurrency:** observations are local; success and failure publication use
  fresh records and unchanged exact reservation predicates.
- **Persistence:** explicit codec includes only validated telemetry; old rows
  remain compatible; restart/export are covered.
- **Privacy:** durable/API/log values are limited to numeric metrics and typed
  allowlisted diagnostics. No raw provider or exception payload is persisted.
- **Snapshots/evidence:** task, baseline, archived task state, and execution
  calls remain unchanged on managed planning failure; SQLite immutable ledger
  checks and focused regressions pass.

## Concerns

- Existing Ruff formatting drift remains in the four amended source files;
  formatting them would create broad unrelated changes. Ruff lint, the new test
  file format check, and `git diff --check` pass.

---

## Fix Round 1

### Findings addressed

1. `EngineeringRuntime.continue_submission()` attached successful plan telemetry
   to its reservation-validated record, but `AgentExecutor.propose()` then
   reloaded the conversation from SQLite and discarded that in-memory field.
2. `ConversationManager._run()` held the conversation `RLock` around the whole
   managed operation, so the coordinator's intended unlocked planner inference
   was still serialized by the outer manager lock.

### Changes

- Added an optional keyword-only `planning_telemetry` input to
  `AgentExecutor.propose()`. The executor applies it to the freshly reloaded
  conversation that is saved in the same proposal transaction.
- The coordinator passes the observation only after its existing exact
  reservation checks succeed.
- Added a default-locked `lock_operation` control to the manager's private
  scheduling path. Managed message planning opts out of the outer lock;
  execution decisions and direct/private calls retain the existing default
  lock.
- Replaced the prior false-positive supersession test. The planner now waits on
  an event without a timeout. A competing thread must acquire the conversation
  lock and durably publish newer state before the planner is released.
- Added a real managed plan-producing API test using the current schedulers. It
  verifies telemetry in the authenticated response and equality after reopening
  the SQLite store.

### RED

Command:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest \
tests/test_planning_telemetry.py::test_managed_plan_success_exposes_telemetry_and_survives_restart \
tests/test_planning_telemetry.py::test_blocked_managed_planner_releases_lock_and_cannot_overwrite_superseding_state \
-q --tb=short
```

Output:

```text
FF                                                                       [100%]
E   TypeError: 'NoneType' object is not subscriptable
E   AssertionError: managed planning held the conversation lock across planner inference
2 failed in 1.53s
```

The first failure proved the executor reload discarded telemetry. The second
proved the manager's outer lock prevented superseding publication while the
planner was blocked.

### GREEN

Targeted command:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest \
tests/test_planning_telemetry.py::test_managed_plan_success_exposes_telemetry_and_survives_restart \
tests/test_planning_telemetry.py::test_blocked_managed_planner_releases_lock_and_cannot_overwrite_superseding_state \
-q --tb=short
```

```text
..                                                                       [100%]
2 passed in 0.47s
```

Final focused verification:

```text
PATH=/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH \
TMPDIR=$PWD/.tmp python -m pytest \
tests/test_planning_telemetry.py tests/test_scheduler_runtime.py \
tests/test_scheduler_web.py tests/test_scheduler_recovery.py \
tests/test_sqlite_store.py tests/test_agent_web.py tests/test_agent_history.py \
tests/test_agent_recovery.py tests/test_copilot.py -q
ruff check src/flight_agent/verification/agent_runtime.py \
src/flight_agent/verification/coordinator.py \
src/flight_agent/verification/routes.py tests/test_planning_telemetry.py
ruff format --check tests/test_planning_telemetry.py
git diff --check
```

```text
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 92%]
..................                                                       [100%]
234 passed in 23.49s
All checks passed!
1 file already formatted
```

### Concurrency proof

The replacement test establishes this order:

1. managed planner enters and blocks indefinitely on `release_planner`;
2. a separate publisher acquires the same conversation lock;
3. the publisher saves a newer pending decision and telemetry;
4. only after publication is observed does the test release the planner;
5. exact reservation revalidation rejects the stale planner result.

The test was also run ten consecutive times:

```text
10 deterministic concurrency runs passed
```

The saved record retains the newer status, pending decision, and telemetry, and
contains no stale assistant answer.

### Self-review

- **Reservation/concurrency:** the manager releases only its outer planning lock;
  coordinator admission and result publication remain protected by their
  existing short lock scopes and exact reservation predicates.
- **Persistence:** successful plan telemetry is assigned to the executor's
  freshly reloaded record immediately before its existing atomic save.
- **Snapshots:** no task, baseline, capability snapshot, project snapshot, or
  immutable evidence path was changed; the executor's existing validation and
  save flow remains authoritative.
- **Nonplanning operations:** manager `_run()` and `_schedule()` remain locked by
  default. Only managed message planning passes `lock_operation=False`.
- **Privacy:** no telemetry shape or logging behavior changed; only validated
  `PlanningTelemetry` is transferred.

### Concerns

- No new concerns. Existing source-file Ruff formatting drift remains outside
  this surgical fix; Ruff lint, the focused test format check, and diff checks
  pass.
