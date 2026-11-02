# Stage 3 Final Review Fix Report

## Finding

`EngineeringRuntime.continue_decision()` opened the store transaction before
restoring the queued admission and calling `_queue_decision()`. The first
`save()` issued `BEGIN IMMEDIATE`, so confirm structure, budget, binding,
resource, boundary, and directory preflight ran while SQLite's global writer
was held. A slow resource check could therefore block unrelated conversation
writes until the busy timeout.

## RED evidence

The deterministic regression blocks conversation A in `resources.verify()`
after managed confirm admission, then attempts to persist a state transition
for conversation B.

Initial focused run:

```text
FAILED tests/test_scheduler_runtime.py::test_confirm_preflight_does_not_hold_sqlite_writer
1 failed in 1.67s
```

The failure was the intended assertion:

```text
an unrelated conversation writer was blocked by confirm preflight
```

A second stale-reservation regression changed the admitted record during
preflight and asserted that consent, baseline hash, and action audit could not
be committed from the stale snapshot. Before the fix:

```text
2 failed in 1.73s
```

The writer remained blocked, and the stale confirm completed instead of
raising `The queued decision is no longer current`.

## Design and atomicity

The executor's existing confirm, resume, and retry validation/mutation logic
was extracted into private preparation methods. Preparation operates on an
in-memory conversation and performs all resource, filesystem, evidence, and
validation work without saving and without an SQLite write transaction.

Managed continuation now:

1. Holds the existing per-conversation lock and captures the exact queued
   admission reservation.
2. Restores the origin only on a deep in-memory copy.
3. Runs the action preflight and prepares the complete mutation on that copy.
4. Reloads and compares the full persisted conversation with the exact
   reservation.
5. Persists the prepared mutation with one `store.save()`, whose short SQLite
   transaction atomically publishes the baseline/action audit and
   `queued_action_committed=True`.
6. Starts execution only after that atomic save.

There is no observable persisted restore/commit gap. A changed reservation is
rejected before publication, and the stale test proves that confirmation,
baseline fingerprint, and decision audit remain absent. Direct confirm,
resume, and retry use the same extracted preparation methods, preserving
consent/version checks, baseline hashing, action audits, completion evidence
verification, retry node reset, and existing duplicate-admission behavior.

The `queued_action_committed` marker is still written in the same atomic
mutation as the action audit, so Task 6 recovery semantics and manager
worker-start/publication-failure handling remain unchanged.

## Changed files

- `src/flight_agent/verification/agent_runtime.py`
  - Extracted private confirm/resume/retry preparation from persistence.
  - Retained the existing public methods and execution behavior.
- `src/flight_agent/verification/coordinator.py`
  - Removed the long outer transaction from `continue_decision()`.
  - Added in-memory preparation, exact reservation revalidation, and one
    atomic save.
- `tests/test_scheduler_runtime.py`
  - Added the blocked-resource/unrelated-writer regression.
  - Added stale-reservation/no-partial-consent-or-audit coverage.
- `.superpowers/sdd/2026-10-31-scheduler-isolation/stage3-final-fix-report.md`
  - Recorded the final-review fix and validation evidence.

## GREEN and validation evidence

Focused new regressions:

```text
2 passed in 0.47s
```

Task 6 runtime/recovery:

```text
53 passed in 4.85s
```

Task 6 manager, Task 8 runtime/web, and broad SQLite/recovery regressions:

```text
223 passed in 14.43s
```

Stage 3 acceptance matrix:

```text
154 passed in 6.91s
```

SQLite transaction/cutover/schema/store/runtime/snapshot tests:

```text
83 passed in 2.76s
```

Complete software-only suite with project-local `TMPDIR`:

```text
1806 passed, 54 skipped, 26 deselected in 164.81s
```

Static and packaging gates:

```text
Ruff --no-cache --no-respect-gitignore src tests: all checks passed
mypy src: success, no issues in 69 source files
python -m build: exit 0
git diff --check: exit 0
```

Browser tests were not run because no browser/UI surface was affected.

## Self-review

- No planner, provider, capability, resource, filesystem, evidence, lane wait,
  or conversation wait occurs under the coordinator's SQLite write
  transaction.
- Same-conversation serialization remains the shared lock registry.
- Different conversations can persist while confirm resource preflight is
  blocked.
- Confirm consent/version checks and baseline hashing are unchanged.
- Resume completion verification and retry actionability/reset behavior remain
  centralized in the same executor paths.
- The full reservation comparison prevents committing a prepared mutation
  after any persisted admission field changes.
- The atomic save contains the action mutation, audit, and
  `queued_action_committed` marker together.
- No public API, dependency, scheduler abstraction, architecture, or Stage 4
  work was added.
- No open concerns were found.
