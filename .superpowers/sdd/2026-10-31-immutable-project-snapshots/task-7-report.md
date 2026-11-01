# Stage 4 Task 7 Report

## Status

DONE

Base:
`c174687dbde23db3692bd1b69b1dec85512de8e1`

Implementation commit:
`cf967a95f36d7a1d894f3d620d77bf207a8d5e01`

## TDD evidence

### RED

The focused cleanup tests were created before production changes and run with
the selected Python, `RUN_MATLAB_TESTS=0`, and a repository-local resolved
`TMPDIR`:

```text
python -m pytest tests/test_project_snapshot_cleanup.py -q --tb=short
12 failed
```

All failures were feature-specific: `SQLiteConversationStore` lacked
`cleanup_project_snapshots()`, and `ProjectSnapshotStore` lacked `cleanup()`.

Self-review then added a nested-staging-symlink containment test before its
production fix:

```text
1 failed
ValueError: Refusing to remove a symlinked managed directory
```

The failure proved the initial implementation quarantined an unsafe staging
directory before classifying it as skipped.

### GREEN

Focused cleanup suite:

```text
13 passed
```

Cleanup/store/reference suite:

```text
69 passed, 2 skipped
```

## Lock and transaction lifetime

`SQLiteConversationStore.cleanup_project_snapshots()` constructs the managed
snapshot store and acquires its exclusive cross-process lifecycle guard before
reading retention state.

While that exclusive guard remains continuously held:

1. `_read()` opens a consistent SQLite read transaction.
2. `retained_snapshot_ids()` validates every persisted current and archived
   task baseline against the complete reference index and returns the retained
   snapshot IDs.
3. `_read()` exits, rolls back the read transaction, and closes its connection.
4. `ProjectSnapshotStore.cleanup()` verifies, hashes, quarantines, and removes
   eligible managed entries with no SQLite transaction open.
5. The exclusive lifecycle guard is released only after cleanup returns or
   raises.

Any reference-index mismatch, malformed baseline, migration/read failure, or
SQLite error therefore aborts before filesystem deletion. There is no empty or
partial-retention fallback.

The deterministic process test publishes in a spawned process while holding
the shared lifecycle guard through `store.save(record)`. Parent-process cleanup
attempts the exclusive guard and cannot enter `ProjectSnapshotStore.cleanup()`
until the reference commit completes and the shared guard is released. Cleanup
then reads the committed reference and retains the object. The thread-level
barrier test covers the same publication/reference interval without sleeps.

## Enumeration and deletion containment

- Cleanup enumerates only `snapshots/objects` and `snapshots/staging`.
- It never enumerates sibling `work`, source, result, delivery, output, or
  Worker roots.
- Every shard, object, and staging entry is classified with `lstat()` and
  reparse metadata before traversal.
- Only two-lowercase-hex shards and matching 64-hex object directories are
  candidates.
- Unknown names, symlinks, reparse points, special files, and wrong-shard
  identities are reported in `skipped`.
- Candidate objects must have canonical manifests, matching content identity,
  exact sealed inventory, read-only modes, and independent regular files.
- Deletion re-verifies the object and reuses Task 2 same-parent,
  identity-checked `_quarantine_and_remove()`; direct recursive deletion was
  not restored.
- Valid abandoned `snap-<32 hex>` staging directories are recursively
  preflighted without following links and then use the same quarantine/removal
  path.
- Linked external files, nested staging symlinks, sources, deliveries, and
  mutable work markers remain untouched.

Dry-run is the default. It records sorted `retained`, `would_delete`,
`staging`, and `skipped` values without removing objects or staging
directories. Explicit `dry_run=False` records `deleted` only after successful
quarantine and verified removal. Deletion errors propagate and are never
reported as success.

## Corrupt and staging behavior

- Corrupt manifests and valid manifests in writable/unsealed objects are
  skipped and retained on disk.
- Object symlinks, mocked Windows-style reparse objects, unknown shards,
  special shard entries, and invalid object names are skipped without
  traversal.
- Eligible staging directories are listed in both modes and removed only in
  execute mode.
- Staging trees containing nested symlinks or other non-owned members are
  skipped before quarantine.
- A missing reference-index row aborts the complete operation before any
  referenced object can be deleted.

## Changed files

- `src/flight_agent/verification/project_snapshots.py`
  - Added `SnapshotCleanupReport`, content-only verification/removal, contained
    object/staging enumeration, default-preserving object removal, and
    quarantine-backed cleanup.
- `src/flight_agent/verification/sqlite_store.py`
  - Added the default-dry-run SQLite retention entrypoint under the exclusive
    lifecycle guard.
- `tests/test_project_snapshot_cleanup.py`
  - Added dry-run/execute, archived retention, corruption, reparse/symlink,
    unknown-entry, staging, reference-drift, external-link, thread race, and
    spawned-process contention coverage.
- `.superpowers/sdd/2026-10-31-immutable-project-snapshots/task-7-report.md`
  - Recorded Task 7 evidence and reasoning.

## Validation gates

All pytest commands used:

```text
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python
RUN_MATLAB_TESTS=0
TMPDIR=<resolved worktree>/.test-tmp-task7-<gate>
```

Results:

```text
Cleanup/store/reference: 69 passed, 2 skipped
Stage 4 confirmation/execution: 329 passed, 17 skipped
SQLite/recovery/scheduler: 239 passed
Full software: 1934 passed, 82 skipped
Ruff --no-cache --no-respect-gitignore src tests: all checks passed
mypy src: no issues in 74 source files
git diff --check: passed
```

No dependency was installed. Licensed MATLAB remained opt-in and was not run.

## Commit

```text
cf967a95f36d7a1d894f3d620d77bf207a8d5e01
feat: clean only unreferenced managed project snapshots
```

The implementation commit includes the required trailers. The report is
committed separately without amend, push, or merge.

## Self-review

- Confirmed `retained_snapshot_ids()` is the only retention authority and
  covers persisted current plus archived tasks regardless of terminal state or
  external delivery.
- Confirmed the SQLite read transaction ends before manifest/tree hashing and
  filesystem deletion while the exclusive lifecycle guard remains held.
- Confirmed referenced objects are never verified for deletion or removed.
- Confirmed only canonical, sealed, unreferenced managed objects can enter
  `would_delete`.
- Confirmed all execute paths preserve Task 2 quarantine and ownership checks.
- Confirmed valid staging cleanup cannot coexist with an active compliant
  creator because the exclusive lifecycle guard excludes shared publication.
- Confirmed corrupt, unknown, symlinked, reparse, special, unsealed, and linked
  entries fail closed without external traversal.
- Confirmed cleanup does not inspect filesystem timestamps, `recent()`, task
  status, result paths, output roots, source locators, or Worker locators.
- Confirmed no schema, dependency, CLI, HTTP, retention expiry, task purge,
  pin-release, push, merge, or amend was introduced.

## Concerns

No known in-scope software concern remains. Live Windows and licensed MATLAB
were not available; existing deterministic Windows API/reparse tests and all
software-safe gates passed.

## Fix round 1/5: reject cleanup inside an active store transaction

### Finding

`SQLiteConversationStore._read()` deliberately reuses the thread-local SQLite
connection when one exists. Before this fix, `cleanup_project_snapshots()`
could therefore read uncommitted retained-reference state and hash or delete
filesystem objects while an outer `store.transaction()` remained active. A
later rollback could restore SQLite references after their snapshot objects
had already been removed.

### Strict TDD evidence

The active-transaction regressions were added before the production guard.
They cover default dry-run, explicit execute mode, an existing referenced
object, an eligible unreferenced object, and an eligible staging directory.

RED:

```text
python -m pytest tests/test_project_snapshot_cleanup.py \
  -k 'active_transaction or outer_rollback' -q --tb=short
3 failed
```

Each failure was the expected missing rejection:

```text
Failed: DID NOT RAISE ValueError
```

GREEN after the single production change:

```text
python -m pytest tests/test_project_snapshot_cleanup.py -q --tb=short
16 passed
```

### Fix

`SQLiteConversationStore.cleanup_project_snapshots()` now checks the
thread-local `transaction_depth` as its first operation and raises:

```text
ValueError: Project snapshot cleanup requires no active transaction
```

The check occurs before constructing `ProjectSnapshotStore`, acquiring or
creating the lifecycle guard/layout, opening any read connection, enumerating
managed storage, hashing content, or deleting anything. Cleanup does not open a
second connection and does not commit, roll back, or close the caller's
transaction.

### Regression guarantees

- Both dry-run and execute cleanup reject from an active outer transaction.
- The referenced object, eligible orphan, and eligible staging directory all
  remain present.
- Baseline JSON and the complete reference-index rows remain byte-for-byte
  unchanged.
- The dry-run caller can continue using the same outer transaction and commit
  subsequent conversation changes.
- The execute-mode caller can continue using the same outer transaction and
  independently roll it back.
- Instrumentation proves the snapshot lifecycle guard is never entered.
- A store without a snapshot root remains without one after rejection.

### Validation gates

All pytest commands used the selected Python, `RUN_MATLAB_TESTS=0`, and a
repository-local resolved `TMPDIR`.

```text
Cleanup/reference/store: 72 passed, 2 skipped
SQLite transaction regressions: 42 passed
Stage 4 snapshot regressions: 329 passed, 17 skipped
Full software: 1937 passed, 82 skipped
Ruff --no-cache --no-respect-gitignore src tests: all checks passed
mypy src: no issues in 74 source files
git diff --check: passed
```

### Fix commit

```text
49ec734070af1338df91a1604e5ed25afa8998c4
fix: reject snapshot cleanup in active transactions
```

The fix commit includes the required trailers. This report update is a
separate commit; no amend, push, or merge was used.

### Fix-round self-review

- Confirmed every positive `transaction_depth`, including a transaction whose
  write connection has not yet been opened, rejects cleanup.
- Confirmed rejection precedes all snapshot-store construction and guard/root
  side effects.
- Confirmed normal cleanup outside a transaction retains the original
  exclusive-guard and short independent SQLite-read lifetime.
- Confirmed no behavior of `_read()`, nested savepoints, caller-owned
  transaction commit/rollback, schema, CLI, HTTP, or retention policy changed.
- Confirmed the fix is thread-local to the store transaction model and does
  not weaken the cross-process lifecycle race protection.

### Fix-round concerns

No known in-scope concern remains. Live Windows and licensed MATLAB were not
run; all software-safe gates passed.
