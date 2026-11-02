# SQLite conversation state

The active v2 conversation database is
`runs/verification/conversations.sqlite3`. Conversation directories remain
artifact roots; the service no longer reads or writes `conversation.json`.
Worker request/status files, invocation receipts, checkpoints, and evidence
remain disk-backed JSON or binary files.

## First upgrade

1. Stop the old Web/Agent process before deploying Stage 2.
2. Run the existing `cleanup-legacy` command in dry-run mode and review its
   report. Apply it only when the positively identified v1 candidates may
   be removed. Do not delete unknown records or source/delivery directories.
3. Start the new application. Before creating its first database, it copies
   the managed run root to a sibling
   `verification.pre-sqlite-<32-hex-id>` directory and verifies every copied
   regular file's size and SHA256. A symlink, failed copy, changed source
   inventory, or digest mismatch stops startup.
4. Preserve that backup. Historical JSON stays untouched but is not active
   history and is never imported. New conversations use SQLite only.

For an explicitly empty development root, construct
`SQLiteConversationStore(root, require_empty=True)`. It rejects all existing
entries and never clears a directory for you. Normal production startup
uses the verified-backup path, not an environment-variable deletion flag.

## Database lifecycle and failures

The store configures WAL, foreign keys, a 5000 ms busy timeout, and FULL
synchronous writes. Applied migration versions and statement digests are
checked before requests can be accepted. Unknown newer schemas, a changed
migration ledger, invalid state, or migration failure stop startup; there
is no automatic downgrade, destructive recreation, or JSON fallback.

Only one application process owns this managed local root. Do not run
multiple Web/Agent processes against it. Stage 2 retains the existing
single-operation admission behavior; it does not add schedulers.

Startup and cleanup are offline, cooperative single-owner operations. The
startup identity guard rejects pathname replacement before or during open,
but standard-library `sqlite3` cannot pin the opened file. A non-cooperating
process can ignore the advisory lock and replace the pathname after the last
identity check but before the first PRAGMA. Outside the single-owner contract,
startup is unsafe; complete resistance requires a controlled native
VFS/adapter.

Startup changes unresolved running calls/nodes to interrupted and preserves
their call IDs, confirmed baselines, specs, checkpoint hashes, and budgets.
Use explicit resume to reconcile the original operation. Unknown execution
must not be retried or replaced with a newly dispatched call.

## Deterministic support export

Stop the service before using this offline example: constructing the store
performs startup recovery. The example prints an index, not evidence bytes.

```python
from pathlib import Path
from flight_agent.verification.sqlite_store import SQLiteConversationStore

store = SQLiteConversationStore(Path("runs/verification").resolve())
record = store.recent()[0]
print(store.export(record.id).decode("utf-8"), end="")
```

Export uses one read snapshot, explicit row ordering, canonical UTF-8 JSON,
and no export-time timestamp. Re-export without intervening writes produces
identical bytes. Missing or tampered indexed evidence fails export; it is
not silently omitted. Review/redact the resulting state before sharing it.

## Backups after SQLite activation

Stop the service and keep the database, its WAL/SHM sidecars if present,
managed artifact directories, and separately selected external task-result
directories together in the recovery set. Alternatively use SQLite's
`Connection.backup()` for a consistent metadata copy and separately back up
evidence while execution is stopped. Copying only the live main database
file is not a complete WAL backup.

The pre-cutover backup contains only the managed run root. It does not
traverse or modify external source projects or external delivery roots.
Operators must preserve external delivery evidence separately when required.

## Rollback

Before release, revert the Stage 2 commits if necessary. After SQLite is
active, rolling back to the JSON-store release is unsupported. Preserve the
pre-cutover backup and active database/evidence, diagnose the failure, and
roll forward. Never run cleanup to manufacture an empty root, delete a
database to bypass schema refusal, or overwrite user project/delivery
directories.
