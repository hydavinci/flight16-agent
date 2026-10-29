# Task 3 report

## Status and commits

**COMPLETE** — Task 3 only, on `refactor/v2-runtime-only` in the requested worktree.

- Implementation: `bcaea13aef6c1e896bc8aa633f7581b3742a8697` — `refactor: make conversation contracts v2 only`.
- This report is committed separately as `docs: record Task 3 implementation evidence`; its hash is returned with the implementation hash.
- Both commits carry the requested `Co-authored-by` and `Copilot-Session` trailers.
- No subagents/reviewers, merge, push, or worktree deletion.
- Cleanup was invoked **only by tests using pytest temporary directories**. Neither the real repository `runs/` nor the worktree `runs/` was cleaned.

## Binding ruling implementation

- Generic `contracts.Task` is exactly `AgentTask`. `Conversation` has schema `"2.0"`, v2-only current/archived tasks, and no `scenario`, `goal`, `active_workflow`, or `workflow_agenda`.
- The eight legacy orchestration classes and `LegacyTask` moved to temporary `verification/legacy_contracts.py`. Existing class bodies were preserved exactly, including the legacy diagnosis artifact conversion. Legacy-only callers/tests now import them there.
- No back-import or re-export from generic contracts to the temporary module. A fresh-process import guard verifies that contracts, store, views, and executor import with both `legacy_contracts` and `compat` forbidden.
- Worker transport classes remain unchanged. `DiagnosticRunState` and Task 2 diagnostic implementation files were not changed; their focused tests pass.
- `agent_runtime.has_unresolved_call` now directly implements v2 call-state detection. Its `compat` import is removed. Coordinator imports/legacy branching remain for Task 4.
- Views project v2 tasks directly and accept only schema `"2.0"`.
- Raw classification follows the brief's precedence: explicit v2 envelope; otherwise all concrete tasks explicitly v2; otherwise any positive legacy task scenario; otherwise unknown. Untrusted list/dict scenario values do not crash classification.
- Normalization removes only the four obsolete envelope fields and adds `"2.0"` without mutating input. Store reads are bounded to 8 MB plus one overflow byte, classify before validation, and issue the specified v1/unknown errors. Reading historical waiting records does not rewrite their bytes; saving writes the new envelope.
- Explicit cleanup reports `v1`, `v2`, `unknown`, and `deleted` IDs. It validates IDs, rejects root/candidate/record symlinks, checks resolved containment before reading/deleting, and calls targeted `shutil.rmtree(candidate)` only for positive v1 records with `apply=True`. Invalid JSON/encoding and oversized records remain unknown. Dry runs preserve bytes. External delivery paths and nested symlink targets are not followed or deleted.

## Changed files

All paths below are relative to the worktree.

### Core behavior

- `src/flight_agent/verification/contracts.py` — v2-only task alias/envelope; remove relocated definitions/imports.
- `src/flight_agent/verification/legacy_contracts.py` — temporary legacy-only definitions.
- `src/flight_agent/verification/legacy_cleanup.py` — raw classification, normalization, report, explicit safe cleanup.
- `src/flight_agent/verification/store.py` — bounded JSON/classification/normalization.
- `src/flight_agent/verification/views.py` — v2-only projection/schema.
- `src/flight_agent/verification/agent_runtime.py` — local unresolved-call helper; remove writes to deleted envelope fields.
- `src/flight_agent/verification/routes.py` — delete only the history-list read of removed `record.scenario`; otherwise the required authenticated-history test raises `AttributeError`. No Task 4 route/coordinator redesign.

### Mechanical legacy import relocation

- `src/flight_agent/verification/compat.py` — relocated classes; explicit legacy/v2 annotations instead of generic `Task`.
- `src/flight_agent/verification/runtime.py`
- `src/flight_agent/verification/planner.py`
- `src/flight_agent/verification/tools.py`
- `src/flight_agent/verification/evaluation.py`
- `src/flight_agent/verification/reports.py`
- `src/flight_agent/verification/diagnosis.py`
- `src/flight_agent/verification/diagnosis_reports.py`
- `src/flight_agent/verification/general.py`

### Tests and report

- `tests/test_agent_contracts.py` — complete legacy task rejection, v2 envelope, independent import guard.
- `tests/test_agent_views.py` — replace obsolete generic v1 projection expectations with v2-only schema and current/archive call preservation.
- `tests/test_agent_history.py` — normalize old v2 envelopes without losing history; use v2 helper; construct relocated legacy model directly for retained Worker uncertainty checks.
- `tests/test_legacy_cleanup.py` — classification matrix, normalization, dry/apply deletion, malformed/oversized input, path safety, non-deleting store rejection.
- `tests/test_copilot.py` — legacy contract import.
- `tests/test_diagnosis_runtime.py` — legacy contract imports.
- `tests/test_structured_planner.py` — legacy contract imports.
- `tests/test_verification_evaluation.py` — legacy baseline imports.
- `tests/test_verification_reports.py` — legacy baseline import.
- `.superpowers/sdd/2026-10-29-remove-v1-runtime/task-3-report.md` — this evidence.

Implementation diff: 25 files, 720 insertions, 332 deletions; 211 inserted lines are the temporary legacy module, with eight class bodies mechanically relocated.

## Verification commands and outputs

Every command below ran after:

```bash
cd /Users/yhe/Work/flight-agent/.worktrees/v2-runtime-only
```

The requested interpreter was used throughout. Pytest's configured `pythonpath = ["src"]` selects worktree code. Standalone import commands require `PYTHONPATH=src` because the virtualenv's editable install otherwise points to the main checkout. The subprocess regression test explicitly propagates pytest's `sys.path`.

### Baseline

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_contracts.py tests/test_agent_views.py tests/test_agent_history.py tests/test_agent_recovery.py -q
```

```text
........................................................................ [ 43%]
........................................................................ [ 87%]
.....................                                                    [100%]
165 passed in 1.71s
```

### RED

The brief's three-file command was run before production changes:

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_contracts.py tests/test_agent_views.py tests/test_legacy_cleanup.py -q --tb=no
```

Exact result line (exit 1):

```text
34 failed, 136 passed in 0.87s
```

Expected reasons: old `Conversation` had no v2 envelope marker and accepted complete v1 tasks; raw cleanup did not exist; store did not classify before legacy validation. Earlier `--tb=short` runs also failed; explicit IDs were added to the oversized-byte test parameter to avoid multi-megabyte failure names.

After adding the view/history/import assertions, still before production changes:

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_contracts.py tests/test_agent_views.py tests/test_agent_history.py tests/test_agent_recovery.py tests/test_legacy_cleanup.py -q --tb=no
```

Exact compact output (exit 1):

```text
FFFF.................................................................... [ 36%]
..............................................................F..F...... [ 72%]
....FFFFFF.............FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF.                  [100%]
=========================== short test summary info ============================
FAILED tests/test_agent_contracts.py::test_conversation_envelope_is_v2_without_legacy_orchestration_fields
FAILED tests/test_agent_contracts.py::test_v2_contract_store_view_and_executor_import_without_legacy_orchestration
FAILED tests/test_agent_contracts.py::test_conversation_rejects_complete_legacy_task[task]
FAILED tests/test_agent_contracts.py::test_conversation_rejects_complete_legacy_task[archived_tasks]
FAILED tests/test_agent_views.py::test_task_view_rejects_legacy_schema_version
FAILED tests/test_agent_history.py::test_existing_v2_envelope_normalizes_without_rewriting_persisted_history
FAILED tests/test_agent_history.py::test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call[failed-True-False]
FAILED tests/test_agent_history.py::test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call[interrupted-True-False]
FAILED tests/test_agent_history.py::test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call[failed-False-True]
FAILED tests/test_agent_history.py::test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call[interrupted-False-True]
FAILED tests/test_agent_history.py::test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call[running-True-True]
FAILED tests/test_agent_history.py::test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call[completed-True-True]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[None-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data1-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data2-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data3-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data4-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data5-v1]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data6-v1]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data7-v1]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data8-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data9-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data10-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data11-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data12-v2]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data13-v2]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data14-v2]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data15-v1]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data16-unknown]
FAILED tests/test_legacy_cleanup.py::test_raw_record_classification_requires_positive_evidence[data17-v2]
FAILED tests/test_legacy_cleanup.py::test_normalize_existing_v2_record_removes_only_legacy_envelope_fields
FAILED tests/test_legacy_cleanup.py::test_cleanup_deletes_only_positive_v1_records[False]
FAILED tests/test_legacy_cleanup.py::test_cleanup_deletes_only_positive_v1_records[True]
FAILED tests/test_legacy_cleanup.py::test_cleanup_preserves_unreadable_or_oversized_records[invalid-json]
FAILED tests/test_legacy_cleanup.py::test_cleanup_preserves_unreadable_or_oversized_records[invalid-utf8]
FAILED tests/test_legacy_cleanup.py::test_cleanup_preserves_unreadable_or_oversized_records[oversized]
FAILED tests/test_legacy_cleanup.py::test_cleanup_ignores_invalid_ids_and_nonconversation_files
FAILED tests/test_legacy_cleanup.py::test_cleanup_rejects_symlink_candidates_without_touching_target[directory]
FAILED tests/test_legacy_cleanup.py::test_cleanup_rejects_symlink_candidates_without_touching_target[record]
FAILED tests/test_legacy_cleanup.py::test_cleanup_rejects_symlink_root - Fail...
FAILED tests/test_legacy_cleanup.py::test_cleanup_does_not_follow_nested_symlinks_or_external_output_root
FAILED tests/test_legacy_cleanup.py::test_store_classifies_raw_records_without_deleting_them[data0-Legacy conversation is unsupported; run cleanup-legacy]
FAILED tests/test_legacy_cleanup.py::test_store_classifies_raw_records_without_deleting_them[data1-Conversation schema is unknown]
43 failed, 156 passed in 1.58s
```

### GREEN: required suites

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_contracts.py tests/test_agent_views.py tests/test_agent_history.py tests/test_agent_recovery.py tests/test_legacy_cleanup.py -q
```

```text
........................................................................ [ 36%]
........................................................................ [ 72%]
.......................................................                  [100%]
199 passed in 1.56s
```

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_contracts.py tests/test_agent_views.py tests/test_legacy_cleanup.py -q
```

```text
........................................................................ [ 42%]
........................................................................ [ 85%]
.........................                                                [100%]
169 passed in 0.80s
```

### GREEN: focused legacy imports

```bash
PYTHONPATH=src /Users/yhe/Work/flight-agent/.venv-matlab/bin/python -c 'import importlib; modules=("legacy_contracts", "runtime", "planner", "compat", "diagnosis", "diagnosis_reports", "general", "evaluation", "reports", "tools", "backend", "worker"); [(importlib.import_module("flight_agent.verification." + name), print("OK flight_agent.verification." + name)) for name in modules]; tests=("test_copilot", "test_diagnosis_runtime", "test_structured_planner", "test_verification_evaluation", "test_verification_reports"); [(importlib.import_module("tests." + name), print("OK tests." + name)) for name in tests]'
```

```text
OK flight_agent.verification.legacy_contracts
OK flight_agent.verification.runtime
OK flight_agent.verification.planner
OK flight_agent.verification.compat
OK flight_agent.verification.diagnosis
OK flight_agent.verification.diagnosis_reports
OK flight_agent.verification.general
OK flight_agent.verification.evaluation
OK flight_agent.verification.reports
OK flight_agent.verification.tools
OK flight_agent.verification.backend
OK flight_agent.verification.worker
OK tests.test_copilot
OK tests.test_diagnosis_runtime
OK tests.test_structured_planner
OK tests.test_verification_evaluation
OK tests.test_verification_reports
```

### GREEN: adjacent regression checks

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_verification_evaluation.py tests/test_verification_reports.py -q
```

```text
...........................                                              [100%]
27 passed in 0.21s
```

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_runtime.py tests/test_agent_web.py tests/test_agent_diagnostic_engine.py tests/test_diagnostic_planner.py -q
```

```text
........................................................................ [ 96%]
...                                                                      [100%]
75 passed in 4.43s
```

`git diff --check` exited 0 with no output. An AST comparison against the pre-task commit also exited 0:

```text
8 legacy orchestration classes relocated without behavioral changes
All remaining transport and task classes unchanged except Conversation
```

### Final combined verification

After the implementation commit, all required and adjacent suites were run together:

```bash
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest tests/test_agent_contracts.py tests/test_agent_views.py tests/test_agent_history.py tests/test_agent_recovery.py tests/test_legacy_cleanup.py tests/test_verification_evaluation.py tests/test_verification_reports.py tests/test_agent_runtime.py tests/test_agent_web.py tests/test_agent_diagnostic_engine.py tests/test_diagnostic_planner.py -q
```

```text
........................................................................ [ 23%]
........................................................................ [ 47%]
........................................................................ [ 71%]
........................................................................ [ 95%]
.............                                                            [100%]
301 passed in 5.56s
```

### Intermediate issues resolved

- First implementation run of the five-file command: `17 failed, 182 passed in 1.55s`. Existing executor resets wrote removed workflow fields; history listing read removed `scenario`; standalone subprocess imported the main checkout. The two executor assignments and one history-list entry were removed, and the subprocess now propagates the source path. No coordinator redesign.
- The first standalone import check omitted `PYTHONPATH=src` and hit the main checkout, producing `ModuleNotFoundError: No module named 'flight_agent.verification.legacy_contracts'`. The corrected focused command above verifies the worktree.
- An optional regression invocation used nonexistent `tests/test_agent_diagnostics.py`: `no tests ran in 0.02s` (exit 4). After discovering the actual filenames, the 75-test command above passed.

## Self-review

- Reviewed every changed production/test diff; additional files beyond the brief are required by the binding relocation ruling or the removed envelope fields.
- Mechanically compared relocated class ASTs with the original definitions; all eight match. All other retained classes except the intentionally changed `Conversation` also match.
- Tests use real JSON/filesystem operations under `tmp_path`, including sibling external-output sentinels. Both dry-run and applied deletion are exercised. No test invokes cleanup on project data.
- Mutating classification to treat unknown as v1, deleting during dry run/startup, following candidate/record symlinks, stripping history, or allowing legacy current/archive tasks is covered by assertions.
- V2 recovery still preserves original invocation identity, checkpoints, receipts, budgets, and confirmed baselines in the required recovery suite.
- No `DiagnosticRunState`, v2 diagnostic module, coordinator, bootstrap, CLI wiring, configuration, or documentation outside this report was changed. No Task 4+ implementation.

## Concerns and follow-ups not implemented

- The relocation guarantee is **importability**, not continued v1 execution through the now-v2-only generic `Conversation`/store/views. Obsolete generic v1 view/history acceptance tests were replaced. Legacy runtime/compat paths that require old envelope fields or v1 `TaskView` remain scheduled for Tasks 4–6; the full legacy execution suite was intentionally not claimed green.
- Coordinator still imports the legacy compatibility helper until Task 4, as explicitly ordered.
- Task 6 must delete `legacy_contracts.py` along with its remaining legacy consumers/tests. No generic compatibility shim was added.
- Existing v1/unknown saved records will cause the specified store startup error and require an explicit operator cleanup/review. Cleanup CLI integration belongs to a later task and was not added.
- No blocking Task 3 concern remains. Branch/worktree are retained for subsequent tasks.
