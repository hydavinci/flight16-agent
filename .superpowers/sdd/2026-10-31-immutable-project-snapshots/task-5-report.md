# Stage 4 Task 5 Report

## Status

DONE

Implementation commit:
`5fde482ef60bbfd0664b6c8d6c615df1f1ef8770`

## TDD evidence

### RED

Before production changes:

```text
python -m pytest tests/test_agent_project_snapshots.py -q --tb=short
11 failed, 20 passed
```

The failures were requirement-specific:

- `AgentContext.project_snapshot()` was absent.
- Dispatch entered the backend without a post-admission snapshot guard check.
- External delivery could overlap the managed snapshot/work parent.

During self-review, a second focused RED exposed an implicit legacy upgrade:

```text
python -m pytest \
  tests/test_agent_project_snapshots.py::test_legacy_project_reconcile_does_not_adopt_snapshot_lineage \
  -q --tb=short
1 failed
```

The old persisted spec lacked `project_snapshots`, but recovery still verified
the managed object. The failure was
`Snapshot file type/size mismatch: initialize.m`.

### GREEN

After implementation:

```text
Task 5 runtime/history/evidence/recovery set: 104 passed
Stage 3 scheduler + snapshot regressions: 211 passed, 2 skipped
Combined required focused gate: 315 passed, 2 skipped
Full software suite, RUN_MATLAB_TESTS=0: 1904 passed, 82 skipped
Ruff --no-cache --no-respect-gitignore src tests: passed
mypy src: passed, 74 source files
git diff --check: passed
```

All pytest runs used a repository-local resolved `TMPDIR`.

## Implementation

### Snapshot lineage and identity

- Added `snapshot_lineage()`, `verify_snapshot_lineage()`, and
  `verify_execution_resources()` in `snapshot_binding.py`.
- Invocation numerics now contain complete direct/transitive
  `project_snapshots` only when lineage exists.
- Mutable work paths are excluded from invocation identity.
- Local resources replaced by confirmed snapshots are not re-read; Worker and
  diagnostic/non-snapshot resources remain reverified before every dispatch.

### Context and future work path

- Preserved all required `build_context()` keywords and appended optional
  `snapshots=`.
- Added `AgentContext.work_directory` at:
  `store.root/work/<conversation>/<task>/calls/<call>`.
- The path is validated but neither created nor used for execution.
- `project_snapshot(role)` validates role/fingerprint and returns a fresh
  validated model value.

## Guard placement and release proof

`verify_snapshot_lineage()` is the only new lifecycle-guard owner. Its shared
guard encloses only managed-object verification and is released on return.

- New dispatch:
  - lane admission: `agent_runtime.py:1643`
  - lineage verification: `agent_runtime.py:1694`
  - budget charging: `agent_runtime.py:1726`
  - call persistence: `agent_runtime.py:1728`
  - backend invocation: `agent_runtime.py:1776`
- Recovery reconciliation:
  - lane admission: `agent_runtime.py:1181`
  - final new-style lineage verification: `agent_runtime.py:1272`
  - backend reconciliation: `agent_runtime.py:1277`
- Loaded receipt acceptance verifies new-style lineage at
  `agent_runtime.py:1165`.
- Archived cache sources verify new-style lineage at
  `agent_runtime.py:979`.

The dispatch and reconciliation tests block the scheduler lane and assert no
snapshot guard entry while waiting. Their backend callbacks assert verification
has occurred and the guard is no longer held. Verification runs under the
conversation lock, preserving
`conversation lock -> shared snapshot lifecycle guard`, and outside SQLite
write transactions. No guard spans evidence writes or backend execution.

## Recovery compatibility

New-style calls are identified solely by the original persisted invocation
spec containing `numerics["project_snapshots"]`. They fail closed on missing or
tampered lineage before receipt acceptance, cache reuse, or reconciliation.

Legacy persisted specs remain immutable:

- `_spec_matches()` accepts the original pre-lineage shape without rewriting
  the spec or request.
- Saved completed receipts are validated against their recorded original spec.
- Legacy contexts do not expose managed snapshot bindings.
- Receipt recovery/reconciliation retains the original call ID.
- Unknown termination remains `ExecutionUncertain`; known failure still needs
  explicit retry; no PID signaling or redispatch was added.

## Changed files

- `src/flight_agent/verification/snapshot_binding.py`
- `src/flight_agent/verification/agent_runtime.py`
- `tests/test_agent_project_snapshots.py`
- `.superpowers/sdd/2026-10-31-immutable-project-snapshots/task-5-report.md`

## Self-review

- Confirmed lineage includes resource-free descendants through all ancestors.
- Confirmed tamper failure occurs before a second call or budget charge.
- Confirmed source deletion after call one does not affect call two.
- Confirmed external output roots cannot contain snapshot/work roots.
- Confirmed Task 4 binding immutability and Stage 3 lane/reservation markers are
  unchanged.
- Confirmed no snapshot materialization, dependency, schema, push, merge, or
  amend was introduced.

Concerns: none.
