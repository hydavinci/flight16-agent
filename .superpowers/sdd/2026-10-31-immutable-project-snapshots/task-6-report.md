# Stage 4 Task 6 Report

## Status

DONE

Base:
`3531383dcb67cc6e7d416571982c939578e6d217`

Implementation commit:
`dbf559cbd47802257a90ded8cd87407227f5a772`

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

The snapshot-store diagnostic now describes early type/size tampering as a
digest/type/size mismatch so all immutable-object corruption fails with the
required digest classification.

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
