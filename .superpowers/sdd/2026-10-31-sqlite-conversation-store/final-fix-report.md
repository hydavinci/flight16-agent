# Stage 2 Final Fix Report

Date: 2026-11-01
Base: `6a7e697782c680d108e47f4153d8eb5f12e5cc7b`
Worktree: `/Users/yhe/Work/flight-agent/.worktrees/stages-2-6`

## Result

All four final-review findings were fixed in one wave:

1. Existing artifact ledger rows are immutable. Checkpoint progress now publishes
   content-addressed immutable files and appends descriptor history instead of
   overwriting prior rows. Completed/partial terminal task payloads are immutable;
   only the current-to-archived position transition is allowed. Terminal header
   recovery verifies evidence without rewriting the task aggregate.
2. Existing databases validate every conversation through `decode()` and the domain
   model in the isolated startup snapshot before the live database is opened for
   migration/recovery.
3. Save encoding and changed-evidence hashing occur before SQLite obtains the write
   lock. Stable file identity claims are rechecked inside the transaction and again
   immediately before commit.
4. Cutover fsyncs copied files, the copied directory hierarchy bottom-up, and the
   rename parent after the final rename.

## RED evidence

Command:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest \
tests/test_sqlite_store.py \
-k 'replacing_persisted_artifact or completed_task_payload or completed_current_task_can_be_archived or corrupt_completed_conversation or evidence_hashing or evidence_change_between' \
tests/test_sqlite_cutover.py \
-k 'replacing_persisted_artifact or completed_task_payload or completed_current_task_can_be_archived or corrupt_completed_conversation or evidence_hashing or evidence_change_between or backup_fsyncs' -q
```

Observed:

```text
8 failed, 1 passed, 37 deselected in 1.29s
```

The failures demonstrated descriptor replacement, three terminal payload rewrites,
completed-conversation corruption passing startup, write-lock contention during
hashing, an evidence race publishing successfully, and missing directory fsync
support. The archival-only transition already passed.

The first expanded run also exposed a legitimate checkpoint transition that had
previously overwritten one artifact row. A regression was added to require append-only
checkpoint descriptor history, and checkpoint files were made content-addressed.

## GREEN evidence

Focused regressions:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest \
tests/test_sqlite_store.py \
-k 'evidence_hashing or evidence_change_between or checkpoint_transition or replacing_persisted_artifact or completed_task_payload or completed_current_task_can_be_archived or corrupt_completed_conversation' \
tests/test_sqlite_cutover.py \
-k 'evidence_hashing or evidence_change_between or checkpoint_transition or replacing_persisted_artifact or completed_task_payload or completed_current_task_can_be_archived or corrupt_completed_conversation or backup_fsyncs' -q
```

```text
10 passed, 37 deselected in 0.25s
```

Expanded affected suite:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest \
tests/test_sqlite_schema.py tests/test_sqlite_snapshot.py \
tests/test_sqlite_cutover.py tests/test_sqlite_store.py \
tests/test_sqlite_runtime.py tests/test_agent_adapters.py \
tests/test_agent_diagnostic_engine.py tests/test_agent_recovery.py -q
```

```text
149 passed in 8.97s
```

Full non-MATLAB software suite:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest \
-m 'not matlab' -q
```

```text
1649 passed, 53 skipped, 26 deselected in 143.19s
```

Enabled browser suite:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
RUN_BROWSER_TESTS=1 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m pytest \
tests/test_agent_browser.py tests/test_agent_layout_browser.py \
tests/test_agent_column_resize_browser.py tests/test_matlab_project_browser.py -q
```

```text
53 passed in 83.71s
```

Ruff:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m ruff check \
--no-cache --no-respect-gitignore src tests
```

```text
All checks passed!
```

mypy:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m mypy src
```

```text
Success: no issues found in 65 source files
```

Build:

```bash
TMPDIR=$PWD/.stage2-final-fix PYTHONPATH=src RUN_MATLAB_TESTS=0 \
/Users/yhe/Work/flight-agent/.venv-matlab/bin/python -m build
```

Exit code `0`; produced:

```text
flight_matlab_agent-0.1.0-py3-none-any.whl
flight_matlab_agent-0.1.0.tar.gz
```

Diff checks:

```bash
git diff --check
git diff --stat 6a7e697782c680d108e47f4153d8eb5f12e5cc7b
```

`git diff --check` exited `0`.

## Files changed

- `src/flight_agent/verification/agent_runtime.py`
- `src/flight_agent/verification/sqlite_codec.py`
- `src/flight_agent/verification/sqlite_cutover.py`
- `src/flight_agent/verification/sqlite_schema.py`
- `src/flight_agent/verification/sqlite_store.py`
- `tests/test_agent_diagnostic_engine.py`
- `tests/test_agent_recovery.py`
- `tests/test_sqlite_cutover.py`
- `tests/test_sqlite_store.py`
- `.superpowers/sdd/2026-10-31-sqlite-conversation-store/final-fix-report.md`

## Self-review

- Confirmed artifact replacement is rejected before SQL publication.
- Confirmed changing completed task baseline, result, or relational state is rejected,
  while unchanged completed tasks can move from current to archived.
- Confirmed mutable execution checkpoints remain supported without rewriting prior
  descriptors or files.
- Confirmed failed/interrupted tasks retain retry/resume transitions.
- Confirmed terminal conversation-header recovery verifies receipts/evidence without
  adding a decision or changing the terminal task payload.
- Confirmed the aggregate validator runs only against the isolated SQLite snapshot
  before live writable open/recovery.
- Confirmed hashing is outside the write-lock interval and identity checks remain
  bounded inside the transaction and at commit.
- Confirmed cutover durability ordering is file fsync, child-directory fsync,
  partial-root fsync, rename, then rename-parent fsync.
- No dependency or environment manifest changed.

## Concerns

None open. Real MATLAB tests remain intentionally disabled by
`RUN_MATLAB_TESTS=0`; all requested software and enabled browser gates passed.
