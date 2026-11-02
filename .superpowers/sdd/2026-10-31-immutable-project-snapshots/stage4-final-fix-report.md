# Stage 4 Final-Review Fix Report

Date: 2026-11-01

## Status

The two scoped Stage 4 final-review Important findings were fixed together.
This report does **not** claim Stage 4 completion; the requested scoped re-review
still follows.

Base commit:
`98a4e78986f51be6dd7a7b4da219dc82ee338c2c`

Implementation commit:
`6c008d173a3072caa9341faaefccef784c9f1261`

## RED evidence

All pytest commands used:

- `/Users/yhe/Work/flight-agent/.venv-matlab/bin/python`
- `RUN_MATLAB_TESTS=0`
- repository-local resolved
  `TMPDIR=/Users/yhe/Work/flight-agent/.worktrees/stages-2-6/.test-tmp`

The unchanged focused baseline first passed:

```text
python -m pytest \
  tests/test_agent_project_snapshots.py \
  tests/test_project_snapshot_cleanup.py -q

50 passed
```

The first regression batch was added before production changes:

```text
python -m pytest \
  tests/test_agent_project_snapshots.py \
  tests/test_project_snapshot_cleanup.py -q \
  -k 'near_limit_manifest or historical_full_manifest or tampered_compact or verified_publication_residue or retained_verified_object_residue or object_quarantine or staging_quarantine or corrupt_publication_residue or symlinked_publication_residue or reparse_publication_residue'

8 failed, 6 passed, 50 deselected
```

The expected failures showed:

- new invocation specifications still contained the complete manifest;
- a historical pending full-manifest receipt was not followed by compact new
  calls;
- publication, object-quarantine, and staging-quarantine residues were treated
  as unknown;
- retained internal residues were reported as skipped rather than recognized;
- already-quarantined object cleanup never reached direct removal.

One tampered-lineage test initially used an execution-node value where an
`AgentNode` was required. The test setup was corrected before the production
lineage implementation; the compact-lineage size and historical recovery tests
already provided requirement-specific RED failures.

Self-review then exposed a missing completed-receipt recovery check:

```text
python -m pytest \
  tests/test_agent_project_snapshots.py::test_completed_snapshot_receipt_recovery_rejects_managed_object_tamper -q

1 failed
```

The pre-fix recovery returned `completed` after managed snapshot tampering.

A final interruption-path review found that cleanup itself can leave an exact
quarantined publication name:

```text
python -m pytest \
  tests/test_project_snapshot_cleanup.py::test_verified_publication_quarantine_residue_is_reclaimed_directly \
  'tests/test_project_snapshot_cleanup.py::test_retained_verified_object_residue_is_preserved[publication-quarantine]' -q

2 failed
```

Both entries were still treated as unknown before the final production change.

## Implementation summary

### Compact invocation lineage

- Added the strict `ProjectSnapshotLineage` contract containing only:
  - `snapshot_id`;
  - `source_fingerprint`.
- `build_invocation_spec()` now writes compact direct/transitive lineage under
  `numerics["project_snapshots"]`.
- The confirmed `AgentBaseline.project_snapshots` mapping remains the sole
  authoritative holder of each complete `ProjectSnapshot` and canonical
  manifest.
- Invocation matching normalizes compact and historical full-manifest values to
  the same lineage identity before comparing all remaining specification fields.
- Execution, cache verification, unresolved-call recovery, reconciliation, and
  completed-receipt recovery continue to resolve the full binding from the
  immutable baseline and verify the managed manifest and file digests.
- Malformed or tampered lineage fails closed.

### Exact compatibility policy

- New specifications and receipts use compact lineage.
- Already-persisted full-manifest new-style pending specifications and receipt
  files remain readable, recoverable, and unchanged on disk.
- Completed historical full-manifest receipts verify after restart without
  rewriting.
- Marker-free legacy specifications retain the existing receipt-only
  compatibility path and are not upgraded to snapshot semantics.
- Cache fingerprints are not rewritten. A full-manifest historical cache entry
  may miss reuse against a new compact fingerprint; correctness and immutable
  historical receipt verification take precedence over cross-representation
  reuse.

### SQLite boundary regression

The regression builds a 4,990-file synthetic canonical manifest and six
completed calls/receipts. It proves:

- the compact conversation is below the actual
  `SQLiteConversationStore.MAX_CONVERSATION_BYTES = 8_000_000` boundary;
- the equivalent repeated-full-manifest record exceeds that boundary;
- the compact record saves to the real SQLite store and reloads unchanged.

### Cleanup residue validation and deletion policy

Cleanup now recognizes only these exact internal forms:

- object-shard `.publish-<32hex>`;
- object-shard `..publish-<32hex>.quarantine-<32hex>`;
- object-shard `.<64hex-snapshot-id>.quarantine-<32hex>`;
- staging `.snap-<32hex>.quarantine-<32hex>`;
- the pre-existing normal object and `snap-<32hex>` staging names.

Object-like residues must:

- be real non-reparse directories in a valid object shard;
- contain exactly a canonical `manifest.json` and `tree`;
- validate as complete sealed snapshot objects;
- derive a snapshot ID whose prefix matches the containing shard;
- for object quarantine, match the embedded original snapshot ID;
- pass regular-file, single-link, inventory, mode, size, and digest checks.

Retained verified object residues are preserved because they may be the only
recoverable copy. Verified unretained residues are reported in `would_delete`;
destructive cleanup removes them and records their exact residue name in
`deleted`.

Already-quarantined object/publication residues are removed directly after
reverification and directory-identity checks. They are never renamed through a
second quarantine cycle, so an interrupted cleanup cannot create recursively
named quarantine residue leaks.

Staging quarantine residues must match the exact internal name and pass the same
owned directory inventory checks as normal staging cleanup. They are reported in
`staging` and removed directly. Unknown names, corrupt/unsealed trees, symlinks,
reparse points, special files, and linked files remain in place and are reported
in `skipped`.

The existing default dry-run behavior, lifecycle exclusive guard, SQLite active
transaction rejection, retention authority, and report fields remain unchanged.
Documentation now explains that existing report fields may contain exact
internal residue names.

## GREEN and verification results

Focused lineage verification:

```text
python -m pytest tests/test_agent_project_snapshots.py -q \
  -k 'invocation_identity or near_limit_manifest or historical_full_manifest or tampered_compact or new_style_loaded_receipt'

6 passed, 32 deselected
```

Completed receipt recovery verification:

```text
python -m pytest \
  tests/test_agent_project_snapshots.py::test_completed_snapshot_receipt_recovery_rejects_managed_object_tamper \
  tests/test_agent_project_snapshots.py::test_historical_full_manifest_completed_receipts_verify_after_restart -q

2 passed
```

Final focused gate:

```text
python -m pytest \
  tests/test_agent_project_snapshots.py \
  tests/test_project_snapshot_cleanup.py -q

67 passed
```

Final Stage 4 targeted software gate:

```text
python -m pytest \
  tests/test_project_snapshot_contracts.py \
  tests/test_project_snapshot_store.py \
  tests/test_project_snapshot_references.py \
  tests/test_project_snapshot_cleanup.py \
  tests/test_agent_project_snapshots.py \
  tests/test_agent_runtime.py \
  tests/test_agent_recovery.py \
  tests/test_agent_adapters.py \
  tests/test_agent_diagnostic_engine.py \
  tests/test_agent_delivery.py \
  tests/test_local_matlab.py \
  tests/test_local_matlab_adapters.py \
  tests/test_local_matlab_frequency.py \
  tests/test_matlab_project_acceptance.py \
  -m 'not matlab' -q

493 passed, 2 skipped, 4 deselected
```

Final complete software-only suite:

```text
python -m pytest -m 'not matlab' -q

1959 passed, 56 skipped, 26 deselected
```

Static and build gates:

```text
python -m ruff check --no-cache --no-respect-gitignore src tests
All checks passed

python -m mypy src
Success: no issues found in 74 source files

git diff --check
Passed

python -m build
Passed using the normal isolated build environment
```

The wheel contained 90 entries and the sdist contained 170 entries. Neither
contained generated snapshot objects/staging, work directories, SQLite state,
test temporary state, or bytecode.

An additional `python -m build --no-isolation` diagnostic failed because the
existing MATLAB virtual environment does not contain `setuptools`; no dependency
was installed. The required normal isolated build then passed.

Licensed MATLAB was not run. Browser tests were not rerun because this fix has no
browser/UI surface. Live Windows was not available; deterministic reparse mocks
and platform-independent containment tests passed.

## Self-review

The final diff was reviewed for scope, compatibility, lock/transaction
boundaries, fail-closed behavior, and cleanup ownership.

Findings corrected during self-review:

1. Retained normal canonical objects were initially reverified during cleanup.
   The retained fast path was restored before residue validation, preserving the
   previous behavior and avoiding unnecessary full hashing of retained projects.
2. The first cleanup implementation recognized `.publish-*` but not the exact
   `..publish-*.quarantine-*` name that `_cleanup_if_owned()` can leave after an
   interruption. A RED regression and exact pattern handling were added.
3. Completed receipt recovery matched normalized lineage but did not yet verify
   the managed object. A RED tamper regression added baseline-resolved full
   verification to `_verify_saved()`.

No unrelated refactoring was performed. The deferred duplicate pre-reconcile
lineage verification was intentionally left unchanged. No filesystem
copy/hash/delete was moved into a SQLite transaction, and no snapshot lifecycle
guard was extended across scheduler waits, backend/MATLAB execution, or evidence
writes.

## Remaining concerns

- A scoped final re-review is still required; this report does not declare Stage
  4 complete.
- Licensed MATLAB acceptance was not run.
- No live Windows host was available.
- Cross-representation cache reuse is intentionally not guaranteed; historical
  full-manifest receipts remain verifiable and recoverable without rewriting.
