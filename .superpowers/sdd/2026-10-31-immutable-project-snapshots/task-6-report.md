# Stage 4 Task 6 Report

## Status

DONE

Base:
`3531383dcb67cc6e7d416571982c939578e6d217`

Implementation commit:
`dbf559cbd47802257a90ded8cd87407227f5a772`

Initial report commit:
`1cdc8a2dff25ab08ec689d91a0b97f3607238f63`

## TDD evidence

### RED

The five focused behavioral tests were added before production changes and run
with the required Python, `RUN_MATLAB_TESTS=0`, and repository-local resolved
`TMPDIR`:

```text
python -m pytest tests/test_local_matlab.py tests/test_local_matlab_adapters.py \
  -k 'internal_work or only_managed_snapshot or tampered_managed or bare_source or historical_metric_checkpoint' -q

4 failed, 1 passed, 125 deselected
```

The failures were requirement-specific:

- project execution still called `snapshot_matlab_project()` after source
  removal;
- `LocalMatlabBackend` had no managed snapshot store;
- adapter execution had no managed `work_directory`;
- checkpoints had no authenticated `backend_directory` marker.

### GREEN

Focused GREEN:

```text
5 passed, 125 deselected
```

Listed fake-Engine acceptance set:

```text
439 passed, 4 deselected
```

Task 5 snapshot-lineage plus Stage 3 scheduler/recovery regressions:

```text
205 passed
```

Final software-only gate:

```text
1909 passed, 82 skipped
Ruff: all checks passed
mypy: no issues in 74 source files
git diff --check: passed
```

Licensed MATLAB remained opt-in and was not run.

## Request compatibility

- `LocalMatlabRequest.project_snapshot` binds new project-bearing requests to
  the confirmed immutable inventory.
- The wrap serializer omits `project_snapshot` when it is absent, preserving
  historical request JSON and request fingerprints.
- Original `project`, inputs, selected entries, settings, conditions, and
  evidence remain unchanged provenance.
- Artifact-only requests reject snapshot bindings.
- Snapshot bindings must match the original project fingerprint and complete
  file inventory.
- New project-bearing requests without a binding fail before creating a run
  directory or starting Engine.
- Resume accepts an absent binding only for historical receipt reconciliation;
  it does not upgrade or rewrite the persisted request.

## Shared-store wiring

- `create_web_app()` derives one `ProjectSnapshotStore` from the actual
  conversation store, or reuses the injected executor's store.
- The same store is assigned to the single local backend used for readiness,
  catalog registration, and plugin execution.
- Newly constructed executors receive that same store explicitly.
- The no-argument `LocalMatlabBackend.from_environment()` injection seam is
  preserved.
- Store construction does not enter a lifecycle guard, so merely opening the
  workbench or inspecting readiness does not create the snapshot directory.

## Guard and materialization lifetime

- The adapter verifies the confirmed binding under a short shared lifecycle
  guard before creating a checkpoint or entering the backend.
- The backend verifies the binding before creating the run directory or
  probing readiness.
- `ProjectSnapshotStore.materialize()` independently guards verification and
  copying into `directory / "project"`.
- The resulting work copy is mutable and independent; immutable objects are
  never used as MATLAB CWD or writable project roots.
- A final short guarded verification occurs after worker completion.
- Resume verifies a recorded binding under a short guard before reading the
  receipt.
- No lifecycle guard spans scheduler waiting, Engine startup/execution,
  checkpoint/evidence publication, or receipt reconciliation.

## Historical marker-free recovery

- New checkpoints authenticate the reconstructed managed backend directory at
  `context.work_directory / "local-matlab"`.
- The marker is compared with the derived path and is never executed as a
  supplied locator.
- A marker-free historical request can reconcile only at
  `context.directory / "calls/<call-id>/local-matlab"`.
- Historical files and absolute-path-bearing metadata are not moved or
  rewritten.
- The historical regression proves the original request ID is resumed once,
  the original Engine start count remains one, and the replacement managed
  work directory is not created.

## Changed files

- `src/flight_agent/verification/matlab_contracts.py`
- `src/flight_agent/verification/adapters/local_matlab.py`
- `src/flight_agent/verification/local_matlab.py`
- `src/flight_agent/verification/project_snapshots.py`
- `src/flight_agent/web.py`
- `tests/snapshot_helpers.py`
- `tests/test_local_matlab.py`
- `tests/test_local_matlab_adapters.py`
- `tests/test_local_matlab_frequency.py`
- `tests/test_matlab_project_acceptance.py`
- `tests/test_local_matlab_integration.py`
- `.superpowers/sdd/2026-10-31-immutable-project-snapshots/task-6-report.md`

The snapshot-store diagnostic distinguishes an early file type/size mismatch
from a same-size content digest mismatch.

## Validation gates

All pytest commands used:

```text
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python
RUN_MATLAB_TESTS=0
TMPDIR=<resolved worktree>/.test-tmp
```

Executed gates:

- focused RED and GREEN;
- listed local MATLAB fake-Engine acceptance set;
- Task 5 snapshot lineage and Stage 3 scheduling/recovery regressions;
- full software suite with browser and licensed MATLAB tests disabled;
- Ruff with `--no-cache --no-respect-gitignore src tests`;
- `mypy src`;
- `git diff --check`.

## Self-review

- Confirmed every registered project-bearing local MATLAB operation obtains
  its binding from `AgentContext.project_snapshot("project")`.
- Confirmed project execution and linearization use separately materialized
  per-call work copies after original source modification or removal.
- Confirmed stepinfo and allmargin continue staging exact verified dependency
  artifacts into their own independent work trees.
- Confirmed original project descriptors remain provenance and snapshot IDs
  are additive outcome details only for new-style requests.
- Confirmed existing input-artifact, conditions, producer metadata, numerical
  validity, receipt digest, cleanup proof, budget, unknown-call, and no-
  redispatch checks remain active.
- Confirmed tampered immutable objects fail before Engine startup.
- Confirmed external evidence directories contain only published evidence, not
  mutable request/state/execution working files.
- Confirmed source changes after binding no longer alter selected execution
  bytes, while pre-confirmation source/resource validation remains upstream.
- Confirmed no schema, dependency, push, merge, or amend was introduced.

## Concerns

No software-gate concerns. Real licensed MATLAB acceptance was not run because
it remains explicitly opt-in.

## Review fix round 1/5

Review fix commit:
the commit containing this report update (separate from both Task 6 commits
listed above; no amend, push, or merge).

### Strict TDD evidence

The seven focused review tests were written or corrected before the review
production changes. The first run used the required interpreter,
`RUN_MATLAB_TESTS=0`, and the repository-local resolved `.test-tmp`:

```text
tests/test_local_matlab.py::test_legacy_request_json_preserves_exact_canonical_identity
tests/test_local_matlab.py::test_tampered_managed_snapshot_never_starts_engine
tests/test_project_snapshot_store.py::test_size_mismatch_reports_type_size_without_claiming_digest_check
tests/test_matlab_project_acceptance.py::test_injected_web_reuses_registered_backend_for_ui_catalog_and_execution
tests/test_matlab_project_acceptance.py::test_injected_runtime_with_registered_local_tools_requires_backend_owner
tests/test_matlab_project_acceptance.py::test_injected_runtime_rejects_backend_snapshot_store_mismatch
tests/test_matlab_project_acceptance.py::test_persisted_marker_free_metric_reconciles_original_directory_after_restart

RED: 5 failed, 2 passed
```

The RED failures were specific to the review:

- `EngineeringRuntime` did not expose ownership of its registered local
  backend;
- injected Web assembly constructed/configured a different backend rather than
  reusing the capability closures' owner;
- missing ownership and mismatched snapshot stores did not fail closed;
- persisted restart coverage could not address the runtime's actual backend
  owner;
- a pure size mismatch incorrectly claimed that digest validation had run.

The literal legacy request and corrected same-size digest-tamper tests already
passed at RED, proving those compatibility/security behaviors did not require a
production change.

After the production changes:

```text
GREEN: 7 passed
```

A later focused rerun after the final typing/import correction also passed all
seven tests.

### Runtime ownership and shared-store wiring

- `EngineeringRuntime` accepts an optional keyword-only
  `local_matlab_backend`, preserving every existing positional constructor.
- Production assembly records the exact backend passed to
  `register_builtin_plugins()`.
- Injected Web assembly reuses that exact object for readiness and catalog
  availability; registered capability closures continue executing through the
  same object.
- The owner is bound once to `executor.snapshots` when its store is absent.
- A different non-`None` store instance fails closed, even if paths could
  appear equivalent.
- An injected runtime registering `matlab_execute_project` without an exposed
  owner fails closed rather than configuring an unused backend.
- A runtime without local MATLAB capabilities may still obtain a separate
  UI/readiness-only backend.
- Binding and bootstrap do not enter a lifecycle guard or create the snapshot
  root. No guard spans Engine/backend execution.

### Legacy request identity

`tests/fixtures/legacy_local_matlab_request.json` is literal canonical
pre-Task-6 JSON with no `project_snapshot`. Current validation reserializes it
byte-for-byte, continues omitting that field, and preserves the independently
fixed expected fingerprint:

```text
1969c54525d004eb70a1ae1d2de3e8ee44cf8201b7897a0bb0a97c149e732feb
```

The assertion does not derive its expected fingerprint from the current
serializer.

### Persisted marker-free restart

The new acceptance test uses the real SQLite-backed Web/runtime store. It:

- persists a completed project call followed by an unknown metric call;
- creates a genuine historical external call directory and receipt;
- removes `backend_directory`, republishes the digest-bound checkpoint, deletes
  the managed call directory, and removes the original source;
- reconstructs Web/runtime from SQLite and resumes through HTTP;
- forbids process spawning during reconciliation;
- proves the original call IDs, request/state/result/checkpoint bytes, budget,
  Engine-start marker, and snapshot inventory remain unchanged;
- proves no replacement managed directory, source/snapshot synthesis,
  redispatch, or new Engine start occurs.

This preserves receipt-only marker-free reconciliation and authenticated
checkpoint/receipt semantics across an actual process-style restart.

### Diagnostic correction

Pure type/size tampering again reports:

```text
Snapshot file type/size mismatch
```

The backend digest-tamper test now changes content without changing size, so
its expected digest diagnostic is reached only after hashing.

### Review-round changed files

- `src/flight_agent/verification/coordinator.py`
- `src/flight_agent/verification/project_snapshots.py`
- `src/flight_agent/web.py`
- `tests/fixtures/legacy_local_matlab_request.json`
- `tests/test_local_matlab.py`
- `tests/test_matlab_project_acceptance.py`
- `tests/test_project_snapshot_store.py`
- `.superpowers/sdd/2026-10-31-immutable-project-snapshots/task-6-report.md`

### Review-round validation gates

All pytest gates used
`/Users/yhe/Work/flight-agent/.venv-matlab/bin/python`,
`TMPDIR=<resolved worktree>/.test-tmp`, and `RUN_MATLAB_TESTS=0`.

```text
Focused review GREEN: 7 passed
Restart/injected regression check: 3 passed
Covering MATLAB/snapshot/recovery/scheduler/SQLite/Web suite:
  912 passed, 34 skipped
Full software-safe suite (RUN_BROWSER_TESTS=0, RUN_MATLAB_TESTS=0):
  1915 passed, 82 skipped
Ruff --no-cache --no-respect-gitignore src tests: all checks passed
mypy src: no issues in 74 source files
git diff --check: passed
```

Licensed MATLAB remained opt-in and was not run. No dependency was installed.

### Review-round self-review

- Confirmed injected readiness, catalog checks, and execution share one exact
  backend/store identity.
- Confirmed fail-closed paths run before an injected application becomes
  usable.
- Confirmed UI-only fallback does not alter runtimes with registered local
  operations.
- Confirmed the legacy fixture has no synthesized snapshot field and its
  canonical bytes/fingerprint are fixed literals.
- Confirmed restart recovery is SQLite-backed, marker-free, digest-bound, and
  receipt-only.
- Confirmed the recovery path neither opens the removed source nor creates a
  new managed snapshot/work directory.
- Confirmed the diagnostic test separates pre-hash size rejection from
  same-size digest rejection.
- Confirmed existing adapter artifact, conditions, metadata, evidence,
  reservation, checkpoint, budget, unknown-call, and no-redispatch checks pass
  unchanged.

### Review-round concerns

No software-gate concerns. Real licensed MATLAB acceptance was not run because
it remains explicitly opt-in.

## Review fix round 2/5

Review fix commit:
the separate commit containing this section and the round 2 implementation
(no amend, push, or merge).

### Strict TDD evidence

The focused registry/owner tests were added before any round 2 production
change. They covered:

- generic typed capability runtime-owner persistence and lookup;
- all four canonical local MATLAB capability names carrying one exact backend;
- injected execution with the runtime-declared owner deliberately removed;
- missing and wrong registered owner values;
- a registry containing only `matlab_linearize`;
- mixed owners across `matlab_linearize` and `matlab_stepinfo`;
- declared-versus-registered owner mismatch;
- derived-owner snapshot-store mismatch.

The valid focused RED run used the required interpreter,
`RUN_MATLAB_TESTS=0`, and repository-local resolved `.test-tmp`:

```text
RED: 9 failed
```

The failures demonstrated the exact production gaps:

```text
Capability.__init__() got an unexpected keyword argument 'runtime_owner'
LOCAL_MATLAB_CAPABILITY_NAMES was absent
declared-owner removal failed with "requires its backend owner"
linearize-only registration constructed an unrelated environment backend
mixed registered owners did not fail
declared-versus-registered mismatch did not fail
store mismatch was masked by dependence on the missing declaration
```

After the minimal registration and Web derivation changes:

```text
GREEN: 9 passed
```

### Capability registration identity

- `Capability.runtime_owner` is optional and source-compatible, and the frozen
  capability declaration records the runtime object at construction.
- `CapabilityRegistry.register()` persists the owner in its private entry
  beside the operation/readiness/reconciliation handlers that captured it.
- `CapabilityRegistry.runtime_owner(name, OwnerType)` is the typed, read-only
  lookup. It returns the exact object by identity, returns `None` for a missing
  owner, and rejects an owner of the wrong runtime type.
- `CapabilityRegistry.registered(name)` provides a non-throwing membership
  check without exposing mutable registry entries.
- No closure introspection or global ownership map is used.

### Canonical local MATLAB set

`LOCAL_MATLAB_CAPABILITY_NAMES` is the single adapter-owned canonical tuple:

```text
matlab_execute_project
matlab_stepinfo
matlab_linearize
matlab_allmargin
```

Every `Capability` returned by `local_matlab_capabilities(backend)` records
that exact `backend` object as its `runtime_owner`. The adapter regression
asserts both the complete canonical name set and identity equality for every
returned capability.

### Injected Web derivation and validation

- Injected Web assembly scans the complete canonical set, not only
  `matlab_execute_project`.
- If no canonical local capability is registered, existing custom runtimes
  remain source-compatible and Web may construct a UI/readiness-only backend.
- If any canonical capability is registered, its registry owner is the sole
  source of truth for readiness, catalog availability, execution, and snapshot
  binding.
- Missing owners, wrong owner types, mixed owner identities, a different
  runtime-declared owner, and a different executor snapshot-store identity all
  fail before the application is returned.
- `EngineeringRuntime.local_matlab_backend` is only a consistency assertion
  when registered local capabilities exist. Removing it from a correctly
  registered injected runtime does not change the derived backend.
- A linearize-only registry derives and binds its registered backend without
  creating an unrelated backend or creating the snapshot root.
- The full injected acceptance test removes the declared owner, forbids
  environment backend construction, and proves readiness, catalog checks, and
  real controlled execution still use the registered exact backend/store.

### Preserved behavior

- The round 1 literal legacy request fixture and fixed fingerprint remain
  unchanged.
- SQLite marker-free historical restart/reconciliation remains receipt-only.
- Type/size and same-size digest diagnostics remain distinct.
- No lifecycle guard spans backend/Engine execution.
- Existing artifact, conditions, metadata, evidence, reservation, checkpoint,
  budget, unknown-call, no-redispatch, snapshot identity/reference, and guard
  semantics remain active.

### Review-round changed files

- `src/flight_agent/runtime/capabilities.py`
- `src/flight_agent/verification/adapters/local_matlab.py`
- `src/flight_agent/web.py`
- `tests/test_agent_capabilities.py`
- `tests/test_local_matlab_adapters.py`
- `tests/test_matlab_project_acceptance.py`
- `.superpowers/sdd/2026-10-31-immutable-project-snapshots/task-6-report.md`

### Review-round validation gates

All pytest gates used
`/Users/yhe/Work/flight-agent/.venv-matlab/bin/python`,
`TMPDIR=<resolved worktree>/.test-tmp`, and `RUN_MATLAB_TESTS=0`.

```text
Focused owner GREEN: 9 passed
Capability registry suite: 55 passed
Covering capability/MATLAB/snapshot/recovery/scheduler/SQLite/Web suite:
  972 passed, 34 skipped
Full software-safe suite (RUN_BROWSER_TESTS=0, RUN_MATLAB_TESTS=0):
  1921 passed, 82 skipped
Ruff --no-cache --no-respect-gitignore src tests: all checks passed
mypy src: no issues in 74 source files
git diff --check: passed
```

Licensed MATLAB remained opt-in and was not run. No dependency was installed.

### Review-round self-review

- Confirmed the immutable registry entry, not a mutable runtime declaration,
  identity-links each handler to its captured backend.
- Confirmed every subset of the canonical local set triggers ownership
  enforcement.
- Confirmed multiple registered local names must use object identity, not
  path/configuration equality.
- Confirmed wrong-type and missing owners fail before readiness or route use.
- Confirmed runtime declaration mismatch cannot redirect UI or execution.
- Confirmed production registration and injection use the same exact backend
  and executor snapshot store.
- Confirmed no snapshot root is created during binding/bootstrap.
- Confirmed non-local custom registries retain their existing construction
  path.

### Review-round concerns

No software-gate concerns. Real licensed MATLAB acceptance was not run because
it remains explicitly opt-in.
