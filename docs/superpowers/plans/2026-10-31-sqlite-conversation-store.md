# SQLite Conversation Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Stage 2: replace v2 conversation JSON rewrites with a versioned SQLite WAL metadata/state ledger, preserving confirmation, recovery, budgets, and disk-backed evidence across interruption and restart.

**Architecture:** Keep `Conversation` and `AgentTask` as the application contracts and put relational encoding, migrations, and transaction ownership behind a `ConversationRepository` protocol. Persist conversation headers, ordered messages, task state, calls, artifact descriptors, and append-only task events separately; keep immutable receipts, checkpoints, and evidence files in their existing task directories. Make the cutover fail closed with a verified pre-SQLite backup or explicitly empty development state, without importing JSON history or introducing scheduling or snapshot behavior.

**Tech Stack:** Python 3.11+, standard-library `sqlite3`, Pydantic v2, existing FastAPI application, pytest, Ruff, mypy; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-29-v2-runtime-optimization-design.md`, especially “SQLite WAL Persistence,” “Error Handling and Recovery,” “Stage 2: Introduce SQLite WAL,” and “Rollback.”

## Global Constraints

- Keep only the generic v2 Agent execution model.
- Preserve the current confirmation, permission, source-attribution, hashing, evidence, recovery, and deterministic-evaluation guarantees.
- Replace whole-record JSON rewrites with transactional SQLite WAL state.
- Dynamically installing or executing model-generated tools.
- Weakening explicit confirmation, effects permissions, path validation, source binding, capability versioning, budgets, or artifact verification.
- Running multiple local MATLAB Engine operations concurrently by default.
- Migrating or preserving v1 conversation history.
- Replacing disk evidence artifacts with database blobs.
- Adding Redis, Celery, or another external service.
- Redesigning the workbench appearance.
- Worker transport and diagnostic computation components that are used by registered v2 capabilities remain supported. Removing v1 orchestration must not remove those capabilities.
- Ambiguous or unreadable records are not deleted automatically. They are reported as unsupported cleanup candidates with their paths and require explicit manual removal.
- After the cleanup release, startup no longer contains v1 deserialization or migration logic.
- SQLite stores metadata and state. Large evidence remains on disk and is referenced by relative path, SHA256, size, media type, and role.
- Domain payload columns use canonical JSON validated by current Pydantic contracts. Frequently queried identity and status fields remain normal columns.
- A user message and the corresponding conversation state transition commit in one transaction.
- A dispatched call record is committed before the external operation starts.
- Terminal call state, invocation receipt metadata, node state, and budget state commit together.
- Artifact files are written atomically before their database rows become visible.
- Database rows never claim a completed artifact that is absent or hash-invalid.
- Startup marks locally running calls interrupted unless their capability has a supported reconciliation path.
- Enable WAL mode, foreign keys, and a bounded busy timeout.
- Apply versioned migrations at startup before accepting requests.
- Refuse startup on a newer unknown schema version.
- Provide deterministic export of a conversation and its artifact index for support and testing.
- Database or migration failures stop startup with a clear error.
- An unknown external execution state remains unretryable until reconciled.
- Existing evidence survives later evaluation or delivery failures.
- Deleting v1 must not broaden exception handling or introduce silent defaults.
- Keep supported Python at 3.11 and newer as declared by the package.
- The workbench remains native HTML, CSS, and JavaScript.
- There is no Node runtime or frontend compilation step.
- Run unit tests excluding the `matlab` marker, Ruff, mypy, and package build.
- Each stage receives targeted tests, full software-only verification, and a separate commit. Later stages may adjust boundaries discovered by earlier dependency inventory, but may not restore v1 compatibility.
- Each stage is a separate commit. Before release, rollback is commit-level.
- After SQLite becomes active, rollback to the JSON-store release is unsupported because v1 history is intentionally discarded and new state is stored in SQLite. Before deploying Stage 2, the application must make a backup copy of the current v2 run directory or require an explicitly empty development state.
- External user project and delivery directories are never modified by rollback or legacy cleanup.

The seven entries beginning “Dynamically” through “Redesigning” above are copied from the spec's **Non-goals** and remain prohibitions, not implementation instructions. Wrapped spec sentences are joined onto one line without changing their words.

---

## Scope, decisions, and execution rules

This document is a plan, not an instruction to execute during plan authoring. Execution starts in `/Users/yhe/Work/flight-agent/.worktrees/stages-2-6`. All commands below run from that directory using the selected Python environment with this package and its existing development extras installed.

**Decision: normalized ledger, not a JSON document in a database.**

| Option | Benefit | Cost / decision |
|---|---|---|
| Put a complete `Conversation` in one SQLite JSON column | Small adapter | Still rewrites history and obscures transaction/ownership boundaries; reject. |
| Separate headers, messages, tasks, calls, and artifact indexes | Append messages/events, update the affected ledger rows, query identity/status directly | Requires an explicit codec and consistency checks; choose. |

**Decision: hard cutover, not an implicit history importer.** Before first database creation, default startup copies and verifies the existing managed run directory to a sibling backup. It leaves the original files untouched but never reads them as active history. `require_empty=True` is the alternative for a deliberately empty development root; it rejects every existing entry, including JSON, artifacts, and unrelated files. There is no v1 or v2 JSON importer, dual write, JSON fallback, or automatic deletion.

**Stage boundary:** retain the current `BackgroundRunner`, `EngineeringRuntime.lock`, `AgentExecutor.lock`, and `BusyError` behavior. Do not introduce worker pools, per-conversation runtime locks, execution lanes, task parallelism, project snapshots, context filtering, Copilot diagnostics, dependency locking, CI, frontend changes, or an application-factory refactor. Short-lived thread-owned SQLite connections and a repository transaction interface leave Stages 3–4 possible without pretending they are implemented.

**Existing symbols verified against this worktree:**

- `verification/store.py`: `ConversationStore`, `WorkerRunStore`, `now()`, `request_digest()`, path helpers, 8,000,000-byte conversation bound, startup interruption.
- `verification/coordinator.py`: `EngineeringRuntime.queue_message()`, `_wait()`, and `continue_submission()` mutate one `Conversation` and call `store.save()`.
- `verification/agent_runtime.py`: `persist_running_call()`, `AgentExecutor._accept()`, `_dispatch()`, `_recover()`, `_progress()`, and `_run()`.
- `runtime/budget.py`: `BudgetAccount._save()` checkpoints **before** assigning `self.state`; `note_progress()` checkpoints **before** `check_limits()` can raise `BudgetExhausted`.
- `runtime/evidence.py`: `persist_agent_run()`, `load_agent_run()`, `agent_receipt_name()`, `describe_evidence()`, and `verify_evidence()` already implement bounded, immutable, disk-backed evidence.
- `verification/contracts.py`: `Conversation`, `ConversationMessage`, `ConversationState`, `AgentTask`, and `PendingDecision`; `ToolCall` is re-exported from `runtime/contracts.py`.
- `runtime/observations.py`: `OutputArtifact` has `path`, `sha256`, `size`, optional `id`, and optional `node_id`; it has **no** media-type or role properties. Infer those two index attributes without changing the domain model.
- `web.py:create_web_app()` currently constructs `ConversationStore(root / "runs" / "verification")`; injected runtimes must not be reconstructed or recovered a second time.

## File map and interfaces

| File | Responsibility |
|---|---|
| Create `src/flight_agent/verification/sqlite_schema.py` | Connection configuration, versioned schema, migration verification/atomic application |
| Create `src/flight_agent/verification/sqlite_cutover.py` | First-open backup or explicit-empty gate; no JSON parsing |
| Create `src/flight_agent/verification/conversation_repository.py` | Application-facing persistence/path/transaction protocol |
| Create `src/flight_agent/verification/sqlite_codec.py` | Canonical validated domain-to-row and row-to-domain conversion |
| Create `src/flight_agent/verification/sqlite_store.py` | CRUD, transaction units, append-only audit, artifact publication checks, restart recovery, deterministic export |
| Modify `src/flight_agent/verification/store.py:1-21,90-201` | Keep Worker storage/utilities; replace JSON conversation class with SQLite re-export |
| Modify `src/flight_agent/verification/agent_runtime.py:1-60,155-190,237-246,337-346,668-867` | Protocol annotation and atomic terminal acceptance/progress |
| Modify `src/flight_agent/verification/coordinator.py:1-34,73-143` | Protocol annotation; retain single-save intake transition |
| Modify `src/flight_agent/web.py:35-76` | Construct the explicitly named SQLite implementation; preserve injection semantics |
| Create `tests/test_sqlite_schema.py` | Migration, WAL/foreign-key/timeout, future-version, rollback tests |
| Create `tests/test_sqlite_cutover.py` | Backup, explicit-empty, failure, symlink, untouched-history tests |
| Create `tests/test_sqlite_store.py` | Codec/CRUD/order/limits, atomic publication, append-only events, export/restart |
| Create `tests/test_sqlite_runtime.py` | Committed-dispatch observation, terminal/budget atomicity, real process-death tests |
| Modify `tests/test_agent_history.py:1-94,134-148` | Replace JSON-runtime compatibility assertions with SQLite history assertions |
| Modify `tests/test_legacy_cleanup.py:447-484` | Keep raw cleanup tests; remove runtime JSON-reader expectations |
| Modify `tests/test_agent_recovery.py:125-136,291-329` | Preserve missing-evidence recovery coverage and update acceptance fault hook |
| Modify `tests/test_conversation_bounds.py:1-20` | Keep 8 MB failure atomicity and assert relational state remains readable |
| Modify `README.md:181-198` | Active database location and operational guide link |
| Create `docs/sqlite-conversation-store.md` | Cutover, backup/export, recovery, failure, and rollback operations |

No changes to Pydantic baseline, permission, receipt, outcome, budget, or resource contracts are required. No package/dependency changes are required.

### Task 1: Versioned schema and safe SQLite connection lifecycle

**Files:**
- Create: `src/flight_agent/verification/sqlite_schema.py`
- Test: `tests/test_sqlite_schema.py`

**Interfaces:**
- Produces: `Migration(version: int, name: str, statements: tuple[str, ...])`
- Produces: `MIGRATIONS: tuple[Migration, ...]`
- Produces: `connect_database(path: Path) -> sqlite3.Connection`
- Produces: `migrate_database(path: Path) -> None`
- Produces: `DatabaseStartupError(RuntimeError)`
- Consumes: standard-library SQLite; Python 3.11 transaction APIs only.

- [ ] **Step 1: Write failing schema/lifecycle tests**

```python
# tests/test_sqlite_schema.py
import sqlite3

import pytest

from flight_agent.verification import sqlite_schema as schema


def test_schema_is_versioned_idempotent_and_wal(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with schema.connect_database(path) as connection:
        before = list(connection.execute("SELECT * FROM schema_migrations"))
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        names = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "conversations", "messages", "tasks", "task_events",
            "calls", "artifacts", "schema_migrations",
        } <= names
    schema.migrate_database(path)
    with schema.connect_database(path) as connection:
        assert list(connection.execute("SELECT * FROM schema_migrations")) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_future_schema_is_refused_without_rewriting_it(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(schema.DatabaseStartupError, match="newer"):
        schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_failed_upgrade_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    broken = schema.Migration(2, "failure-probe", (
        "CREATE TABLE migration_probe (value TEXT)",
        "INSERT INTO missing_migration_table VALUES (1)",
    ))
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, broken))
    with pytest.raises(schema.DatabaseStartupError, match="migration"):
        schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='migration_probe'"
        ).fetchone() is None
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_migration_ledger_drift_is_refused(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_migrations SET sha256=?", ("0" * 64,))
    with pytest.raises(schema.DatabaseStartupError, match="ledger"):
        schema.migrate_database(path)


def test_next_migration_preserves_existing_data(tmp_path, monkeypatch):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE retained_probe (value TEXT)")
        connection.execute("INSERT INTO retained_probe VALUES ('keep')")
    upgrade = schema.Migration(2, "upgrade-probe", (
        "CREATE TABLE upgraded_probe (value TEXT)",
    ))
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, upgrade))
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM retained_probe").fetchone()[0] == "keep"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2


def test_future_ledger_alone_is_refused(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (999, 'future', ?, '2026-10-31')",
            ("f" * 64,),
        )
    with pytest.raises(schema.DatabaseStartupError, match="newer"):
        schema.migrate_database(path)
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_sqlite_schema.py -q`

Expected: collection ERROR importing `flight_agent.verification.sqlite_schema`; no production module exists yet.

- [ ] **Step 3: Add the exact initial schema**

Create `sqlite_schema.py` with these imports, types, and statements. Do not use SQLite JSON functions, `STRICT` tables, or Python 3.12-only `autocommit` arguments.

```python
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class DatabaseStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256("\n".join(self.statements).encode("utf-8")).hexdigest()


MIGRATIONS = (Migration(1, "conversation-ledger", (
    """CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('planning','waiting_for_user','running','completed','failed','interrupted')),
        project_root TEXT,
        resource_location TEXT,
        output_root TEXT,
        pending_decision_json TEXT NOT NULL,
        error TEXT,
        current_task_id TEXT,
        state_json TEXT NOT NULL,
        FOREIGN KEY (id, current_task_id) REFERENCES tasks(conversation_id, id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        archive_position INTEGER,
        baseline_version INTEGER NOT NULL CHECK (baseline_version >= 1),
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        baseline_json TEXT NOT NULL,
        state_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (conversation_id, id),
        UNIQUE (conversation_id, archive_position)
    )""",
    """CREATE TABLE messages (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        position INTEGER NOT NULL CHECK (position >= 0),
        role TEXT NOT NULL CHECK (role IN ('user','assistant')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        response_kind TEXT,
        gaps_json TEXT NOT NULL,
        PRIMARY KEY (conversation_id, position)
    )""",
    """CREATE TABLE calls (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        position INTEGER NOT NULL CHECK (position >= 0),
        node_id TEXT NOT NULL,
        capability TEXT NOT NULL,
        capability_version TEXT,
        baseline_version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('pending','running','completed','failed','interrupted')),
        inputs_json TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        spec_pending INTEGER NOT NULL CHECK (spec_pending IN (0,1)),
        output_json TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        receipt_position INTEGER,
        error TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (task_id, position),
        UNIQUE (task_id, id),
        UNIQUE (task_id, receipt_position)
    )""",
    """CREATE TABLE artifacts (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        slot TEXT NOT NULL CHECK (slot IN ('task','run','output','receipt','checkpoint')),
        position INTEGER NOT NULL CHECK (position >= 0),
        call_id TEXT,
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        size INTEGER NOT NULL CHECK (size >= 0),
        media_type TEXT NOT NULL,
        role TEXT NOT NULL,
        descriptor_json TEXT NOT NULL,
        PRIMARY KEY (task_id, slot, position),
        FOREIGN KEY (task_id, call_id) REFERENCES calls(task_id, id)
    )""",
    """CREATE TABLE task_events (
        sequence INTEGER PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TRIGGER task_events_no_update BEFORE UPDATE ON task_events
        BEGIN SELECT RAISE(ABORT, 'task_events are append-only'); END""",
    """CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events
        BEGIN SELECT RAISE(ABORT, 'task_events are append-only'); END""",
    "CREATE INDEX conversations_recent ON conversations(updated_at DESC, id)",
    "CREATE INDEX calls_status ON calls(status, task_id)",
    "CREATE INDEX task_events_task ON task_events(task_id, sequence)",
)),)


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
    except BaseException:
        connection.close()
        raise
    return connection
```

`artifacts.relative_path` is relative to its task's validated `result_directory`, not to the database file. `descriptor_json` retains `OutputArtifact.id` and `node_id`. Call-only entries are allowed to have task ownership as well; the composite foreign key forbids assigning an artifact to another task's call.

- [ ] **Step 4: Add atomic migration application and refusal**

Append:

```python
def migrate_database(path: Path) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(path)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        supported = MIGRATIONS[-1].version
        if version > supported:
            raise DatabaseStartupError(
                f"Database schema {version} is newer than supported schema {supported}"
            )
        has_ledger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = list(connection.execute(
            "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
        )) if has_ledger else []
        if any(row["version"] > supported for row in applied):
            raise DatabaseStartupError("Database migration ledger contains a newer schema")
        expected = [
            (item.version, item.name, item.sha256)
            for item in MIGRATIONS if item.version <= version
        ]
        if [tuple(row) for row in applied] != expected:
            raise DatabaseStartupError("Database migration ledger disagrees with schema version")
        if [item.version for item in MIGRATIONS] != list(range(1, supported + 1)):
            raise DatabaseStartupError("Database migration versions must be contiguous")
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise DatabaseStartupError("SQLite WAL mode is unavailable")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )"""
        )
        for migration in MIGRATIONS:
            if migration.version <= version:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                (
                    migration.version, migration.name, migration.sha256,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(f"PRAGMA user_version={migration.version}")
        connection.commit()
    except (sqlite3.Error, OSError) as exc:
        if connection is not None:
            connection.rollback()
        raise DatabaseStartupError(f"SQLite migration failed: {exc}") from exc
    except BaseException:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
```

Check both the highest ledger version and `user_version`: the expected-ledger equality above also refuses unknown ledger rows if somebody changes only one version mechanism. Future-version refusal occurs before journal-mode changes or migration DDL.

After release, append a new `Migration` rather than editing an applied migration's statements, name, or digest.

Use individual `execute()` statements inside the explicit transaction, not `executescript()`, which can commit a pending transaction. WAL readers use a stable database snapshot for the duration of their read transaction. These choices use the Python 3.11 API and SQLite's documented WAL semantics. citeturn111160view0turn111160view1

- [ ] **Step 5: Run GREEN and commit**

Run: `python -m pytest tests/test_sqlite_schema.py -q`

Expected: `6 passed`; migration failure leaves neither its probe table nor version 2.

```bash
git add src/flight_agent/verification/sqlite_schema.py tests/test_sqlite_schema.py
git commit -m "feat: add versioned SQLite conversation schema"
```

### Task 2: Fail-closed first-open backup and explicit-empty cutover

**Files:**
- Create: `src/flight_agent/verification/sqlite_cutover.py`
- Test: `tests/test_sqlite_cutover.py`

**Interfaces:**
- Produces: `prepare_cutover(root: Path, *, require_empty: bool) -> Path | None`
- Consumes: `DatabaseStartupError`; existing `safe_directory(path: Path)`.
- Backup names: sibling `<root.name>.pre-sqlite-<32-hex-id>`; temporary backup uses the same name plus `.partial`.
- A returned backup path identifies a completed verified backup, never a partial directory.

- [ ] **Step 1: Write failing cutover tests**

```python
# tests/test_sqlite_cutover.py
import pytest

from flight_agent.verification import sqlite_cutover as cutover
from flight_agent.verification.sqlite_schema import DatabaseStartupError


def test_nonempty_cutover_backs_up_bytes_without_parsing_or_deleting_json(tmp_path):
    root = tmp_path / "verification"
    record = root / ("a" * 32) / "conversation.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"unreadable-history":')
    evidence = record.parent / "tasks" / "kept" / "metric.bin"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"\x00disk evidence\xff")
    backup = cutover.prepare_cutover(root, require_empty=False)
    assert backup is not None
    assert (backup / record.relative_to(root)).read_bytes() == record.read_bytes()
    assert (backup / evidence.relative_to(root)).read_bytes() == evidence.read_bytes()
    assert not (root / "conversations.sqlite3").exists()
    assert not list(tmp_path.glob("*.partial"))


def test_explicit_empty_mode_rejects_nonempty_state_without_modification(tmp_path):
    root = tmp_path / "verification"
    root.mkdir()
    (root / "keep").write_bytes(b"untouched")
    with pytest.raises(DatabaseStartupError, match="empty"):
        cutover.prepare_cutover(root, require_empty=True)
    assert (root / "keep").read_bytes() == b"untouched"
    assert sorted(path.name for path in root.iterdir()) == ["keep"]
    empty = tmp_path / "empty"
    assert cutover.prepare_cutover(empty, require_empty=True) is None
    assert empty.is_dir()


def test_backup_failure_prevents_database_creation(tmp_path, monkeypatch):
    root = tmp_path / "verification"
    root.mkdir()
    (root / "keep").write_text("original")

    def fail(source, target, **kwargs):
        raise OSError("backup device unavailable")

    monkeypatch.setattr(cutover.shutil, "copytree", fail)
    with pytest.raises(DatabaseStartupError, match="backup"):
        cutover.prepare_cutover(root, require_empty=False)
    assert (root / "keep").read_text() == "original"
    assert not (root / "conversations.sqlite3").exists()


def test_backup_refuses_symlinks_without_reading_external_content(tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "source.m").write_text("user source")
    root = tmp_path / "verification"
    root.mkdir()
    (root / "linked-project").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DatabaseStartupError, match="symlink"):
        cutover.prepare_cutover(root, require_empty=False)
    assert (outside / "source.m").read_text() == "user source"
    assert not (root / "conversations.sqlite3").exists()
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_sqlite_cutover.py -q`

Expected: collection ERROR importing `sqlite_cutover`.

- [ ] **Step 3: Implement verified copy, never history normalization**

```python
# src/flight_agent/verification/sqlite_cutover.py
import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from .intake import safe_directory
from .sqlite_schema import DatabaseStartupError


def _inventory(root: Path) -> dict[str, tuple[int, str] | None]:
    result: dict[str, tuple[int, str] | None] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DatabaseStartupError(f"Cutover refuses symlink: {path}")
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            result[name] = None
        elif path.is_file():
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    digest.update(block)
            result[name] = (size, digest.hexdigest())
        else:
            raise DatabaseStartupError(f"Cutover refuses nonregular entry: {path}")
    return result


def prepare_cutover(root: Path, *, require_empty: bool) -> Path | None:
    safe_directory(root)
    root.mkdir(parents=True, exist_ok=True)
    database = root / "conversations.sqlite3"
    if database.is_symlink():
        raise DatabaseStartupError("Cutover refuses database symlink")
    if database.exists():
        if require_empty:
            raise DatabaseStartupError("Development state must be explicitly empty")
        return None
    if require_empty:
        if any(root.iterdir()):
            raise DatabaseStartupError("Development state must be explicitly empty")
        return None
    target = root.with_name(f"{root.name}.pre-sqlite-{uuid4().hex}")
    partial = target.with_name(target.name + ".partial")
    try:
        before = _inventory(root)
        shutil.copytree(root, partial, symlinks=True)
        if _inventory(root) != before or _inventory(partial) != before:
            raise DatabaseStartupError("Cutover backup verification failed")
        for path in partial.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        partial.rename(target)
        return target
    except (OSError, ValueError) as exc:
        raise DatabaseStartupError(f"Cutover backup failed: {exc}") from exc
    finally:
        if partial.exists():
            shutil.rmtree(partial)
```

Run first-open deployment with the old application stopped; this is a single-process managed local state directory, not a multi-process migration protocol. The pre/post inventory detects source changes during the copy. A symlink or special file refuses cutover rather than following it, deleting it, or guessing ownership. Backup copies are intentionally outside the active root and are never scanned by `recent()` or raw-record cleanup.

- [ ] **Step 4: Run GREEN and commit**

Run: `python -m pytest tests/test_sqlite_cutover.py tests/test_sqlite_schema.py -q`

Expected: `10 passed`; failed backup does not create a database, alter originals, or follow external paths.

```bash
git add src/flight_agent/verification/sqlite_cutover.py tests/test_sqlite_cutover.py
git commit -m "feat: guard SQLite cutover with verified backups"
```

### Task 3: Repository interface, relational codec, and atomic persistence

**Files:**
- Create: `src/flight_agent/verification/conversation_repository.py`
- Create: `src/flight_agent/verification/sqlite_codec.py`
- Create: `src/flight_agent/verification/sqlite_store.py`
- Test: `tests/test_sqlite_store.py`
- Read, unchanged: `src/flight_agent/runtime/evidence.py:20-174`
- Read, unchanged: `src/flight_agent/verification/store.py:125-142,171-192`

**Interfaces:**
- Produces: `ConversationRepository` with `root: Path`, `create() -> Conversation`, `get(conversation_id: str) -> Conversation`, `save(record: Conversation) -> None`, `recent() -> list[Conversation]`, `directory(conversation_id: str) -> Path`, `default_task_root(conversation_id: str) -> Path`, `task_directory(conversation_id: str, task_id: str, output_root: str | None = None, *, create: bool = False) -> Path`, `transaction() -> AbstractContextManager[None]`.
- Produces: `SQLiteConversationStore(root: Path, *, require_empty: bool = False)`, `database_path: Path`, `backup_path: Path | None`.
- Produces codec: `canonical(value: object) -> str`, `encode(record: Conversation, existing_artifacts: list[Row] | None = None) -> dict[str, list[Row]]`, `decode(connection: sqlite3.Connection, conversation_id: str) -> Conversation`, `Row = dict[str, str | int | None]`.
- Consumes: Task 1 schema/connection, Task 2 cutover gate, current Pydantic contracts, current evidence verifier.
- Task 4 adds `export()` and startup recovery; do not activate the production alias until Task 5.

- [ ] **Step 1: Write failing repository tests**

```python
# tests/test_sqlite_store.py
import json
import sqlite3

import pytest

from flight_agent.runtime.evidence import describe_evidence
from flight_agent.runtime.observations import OutputArtifact
from flight_agent.verification.contracts import ConversationMessage, PendingDecision
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from flight_agent.verification.store import now
from tests.test_agent_history import agent_task


def test_round_trip_orders_messages_tasks_and_preserves_domain_values(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    record.task.stage = "awaiting_confirmation"
    record.pending_decision = PendingDecision(kind="confirm_baseline", message="Confirm v1")
    record.messages = [
        ConversationMessage(role="user", content="分析", created_at=now()),
        ConversationMessage(
            role="assistant", content="Confirm v1", created_at=now(), response_kind="plan"
        ),
    ]
    store.save(record)
    assert store.get(record.id) == record
    assert not list(store.root.rglob("conversation.json"))
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT baseline_version FROM tasks").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_transaction_rolls_back_message_and_state_and_event_together(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    store.save(record)
    before = store.get(record.id)
    with sqlite3.connect(store.database_path) as connection:
        event_count = connection.execute("SELECT count(*) FROM task_events").fetchone()[0]
    with pytest.raises(RuntimeError, match="power loss"):
        with store.transaction():
            record.messages.append(
                ConversationMessage(role="user", content="next", created_at=now())
            )
            record.status = "planning"
            record.task.stage = "planning"
            store.save(record)
            raise RuntimeError("power loss")
    assert store.get(record.id) == before
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == event_count


@pytest.mark.parametrize("damage", ["missing", "digest"])
def test_bad_new_artifact_never_publishes_any_state(tmp_path, damage):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    path = directory / "metric.json"
    path.write_text('{"rmse":1}')
    record.task.artifacts.append(OutputArtifact(
        **describe_evidence(directory, "metric.json").model_dump()
    ))
    if damage == "missing":
        path.unlink()
    else:
        path.write_text('{"rmse":9}')
    with pytest.raises(ValueError, match="missing|integrity"):
        store.save(record)
    assert store.get(record.id).task is None
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 0


def test_artifact_index_is_metadata_and_task_events_are_append_only(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    path = directory / "metric.json"
    path.write_text('{"rmse":1}')
    record.task.artifacts.append(OutputArtifact(
        **describe_evidence(directory, "metric.json").model_dump(), id="measurement",
        node_id="compute",
    ))
    store.save(record)
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT relative_path, size, media_type, role, descriptor_json FROM artifacts"
        ).fetchone()
        assert row[:4] == ("metric.json", path.stat().st_size, "application/json", "data")
        assert json.loads(row[4])["id"] == "measurement"
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM task_events")


def test_recent_uses_timestamp_then_id_and_bad_ids_do_not_resolve_paths(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    records = [store.create() for _ in range(22)]
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE conversations SET updated_at='2026-10-31T00:00:00+00:00'")
    assert [item.id for item in store.recent()] == sorted(
        [item.id for item in records]
    )[:20]
    for identifier in ("../outside", "A" * 32, "a" * 31, "a" * 33):
        with pytest.raises(KeyError, match="Unknown conversation"):
            store.get(identifier)


def test_oversize_state_preserves_last_committed_record(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    before = store.get(record.id)
    record.messages.append(ConversationMessage(
        role="user", content="x" * 8_000_000, created_at=now()
    ))
    with pytest.raises(ValueError, match="8 MB"):
        store.save(record)
    assert store.get(record.id) == before
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_sqlite_store.py -q`

Expected: collection ERROR importing `SQLiteConversationStore`.

- [ ] **Step 3: Define the application protocol**

```python
# src/flight_agent/verification/conversation_repository.py
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .contracts import Conversation


class ConversationRepository(Protocol):
    root: Path

    def create(self) -> Conversation: ...
    def get(self, conversation_id: str) -> Conversation: ...
    def save(self, record: Conversation) -> None: ...
    def recent(self) -> list[Conversation]: ...
    def directory(self, conversation_id: str) -> Path: ...
    def default_task_root(self, conversation_id: str) -> Path: ...
    def task_directory(
        self, conversation_id: str, task_id: str, output_root: str | None = None,
        *, create: bool = False,
    ) -> Path: ...
    def transaction(self) -> AbstractContextManager[None]: ...
```

The ellipses here are Python `Protocol` method declarations, not omitted implementations. All concrete methods are supplied below. Database connections never appear in application/runtime method signatures.

- [ ] **Step 4: Encode independently persisted records**

```python
# src/flight_agent/verification/sqlite_codec.py
import json
import mimetypes
import sqlite3
from pathlib import Path
from typing import TypeAlias

from ..runtime.evidence import agent_receipt_name, describe_evidence
from ..runtime.observations import OutputArtifact
from .contracts import Conversation

Row: TypeAlias = dict[str, str | int | None]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def encode(
    record: Conversation, existing_artifacts: list[Row] | None = None,
) -> dict[str, list[Row]]:
    data = record.model_dump(mode="json")
    result: dict[str, list[Row]] = {
        name: [] for name in ("conversations", "messages", "tasks", "calls", "artifacts")
    }
    existing = {
        (row["task_id"], row["slot"], row["call_id"]): row
        for row in (existing_artifacts or [])
        if row["slot"] in ("receipt", "checkpoint")
    }
    result["conversations"].append({
        "id": record.id, "created_at": record.created_at, "updated_at": record.updated_at,
        "status": record.status, "project_root": record.project_root,
        "resource_location": record.resource_location, "output_root": record.output_root,
        "pending_decision_json": canonical(data["pending_decision"]),
        "error": record.error, "current_task_id": record.task.id if record.task else None,
        "state_json": canonical({
            key: data[key] for key in ("schema_version", "output_root_reset", "turn_origin")
        }),
    })
    for position, message in enumerate(record.messages):
        result["messages"].append({
            "conversation_id": record.id, "position": position, "role": message.role,
            "content": message.content, "created_at": message.created_at,
            "response_kind": message.response_kind,
            "gaps_json": canonical(message.model_dump(mode="json")["gaps"]),
        })
    tasks = [
        *((task, position) for position, task in enumerate(record.archived_tasks)),
        *([(record.task, None)] if record.task is not None else []),
    ]
    if len({task.id for task, _ in tasks}) != len(tasks):
        raise ValueError("Current and archived task identities must be unique")
    for task, archive_position in tasks:
        value = task.model_dump(mode="json")
        excluded = {
            "id", "baseline", "stage", "result", "calls", "pending_specs",
            "analysis_runs", "artifacts",
        }
        status = (
            task.result.execution_status if task.result is not None
            else "interrupted" if any(node.status == "interrupted" for node in task.nodes)
            else "failed" if any(node.status == "failed" for node in task.nodes)
            else "running" if any(node.status == "running" for node in task.nodes)
            else "completed" if all(node.status == "completed" for node in task.nodes)
            else "pending"
        )
        result["tasks"].append({
            "id": task.id, "conversation_id": record.id, "archive_position": archive_position,
            "baseline_version": task.baseline.version, "stage": task.stage, "status": status,
            "baseline_json": canonical(value["baseline"]),
            "state_json": canonical({key: item for key, item in value.items()
                                     if key not in excluded}),
            "result_json": canonical(value["result"]),
            "created_at": record.updated_at, "updated_at": record.updated_at,
        })
        runs = {run.id: (position, run) for position, run in enumerate(task.analysis_runs)}
        if len(runs) != len(task.analysis_runs):
            raise ValueError("Invocation receipt identities must be unique")
        call_ids = {call.id for call in task.calls}
        if (
            len(call_ids) != len(task.calls)
            or not set(runs) <= call_ids
            or not set(task.pending_specs) <= call_ids
        ):
            raise ValueError("Invocation metadata must belong to unique recorded calls")
        for position, call in enumerate(task.calls):
            entry = runs.get(call.id)
            run = entry[1] if entry is not None else None
            spec = task.pending_specs.get(call.id) or (run.spec if run is not None else None)
            result["calls"].append({
                "id": call.id, "task_id": task.id, "position": position,
                "node_id": call.node_id, "capability": call.tool,
                "capability_version": call.tool_version, "baseline_version": call.baseline_version,
                "status": call.status, "inputs_json": canonical(call.inputs),
                "spec_json": canonical(spec.model_dump(mode="json") if spec else None),
                "spec_pending": int(call.id in task.pending_specs),
                "output_json": canonical(call.output),
                "receipt_json": canonical(run.model_dump(mode="json") if run else None),
                "receipt_position": entry[0] if entry is not None else None,
                "error": call.error, "started_at": call.started_at, "finished_at": call.finished_at,
            })

        def disk_descriptor(slot: str, call_id: str, path: str) -> OutputArtifact:
            previous = existing.get((task.id, slot, call_id))
            if previous is not None and previous["relative_path"] == path:
                descriptor = OutputArtifact.model_validate_json(
                    str(previous["descriptor_json"])
                )
                if slot != "checkpoint" or descriptor.sha256 == task.checkpoint_sha256.get(call_id):
                    return descriptor
            return OutputArtifact(**describe_evidence(
                Path(task.result_directory), path
            ).model_dump())

        def artifact(
            slot: str, position: int, descriptor: OutputArtifact, call_id: str | None = None,
        ) -> None:
            media_type = mimetypes.guess_type(descriptor.path)[0] or "application/octet-stream"
            role = (
                slot if slot in ("receipt", "checkpoint")
                else "report" if descriptor.path.endswith((".docx", ".pdf"))
                else "figure" if media_type.startswith("image/")
                else "data"
            )
            result["artifacts"].append({
                "task_id": task.id, "slot": slot, "position": position, "call_id": call_id,
                "relative_path": descriptor.path, "sha256": descriptor.sha256,
                "size": descriptor.size, "media_type": media_type, "role": role,
                "descriptor_json": canonical(descriptor.model_dump(mode="json")),
            })

        for position, descriptor in enumerate(task.artifacts):
            artifact("task", position, descriptor)
        run_position = 0
        output_position = 0
        for position, run in enumerate(task.analysis_runs):
            descriptor = disk_descriptor("receipt", run.id, agent_receipt_name(run.id))
            artifact("receipt", position, descriptor, run.id)
            for item in run.artifacts:
                artifact("run", run_position, item, run.id)
                run_position += 1
            for item in run.output.artifacts:
                artifact("output", output_position, item, run.id)
                output_position += 1
        for position, (call_id, path) in enumerate(task.checkpoints.items()):
            descriptor = disk_descriptor("checkpoint", call_id, path)
            if descriptor.sha256 != task.checkpoint_sha256.get(call_id):
                raise ValueError("Checkpoint digest does not match the recorded state")
            artifact("checkpoint", position, descriptor, call_id)
    return result
```

The codec reuses unchanged stored receipt/checkpoint descriptors, so a later externally damaged file does not prevent recording an interrupted/failed task.

- [ ] **Step 5: Decode under one database read snapshot**

Append to `sqlite_codec.py`:

```python
def decode(connection: sqlite3.Connection, conversation_id: str) -> Conversation:
    header = connection.execute(
        "SELECT * FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if header is None:
        raise KeyError("Unknown conversation")
    data = json.loads(header["state_json"])
    data.update({
        key: header[key] for key in (
            "id", "created_at", "updated_at", "status", "project_root",
            "resource_location", "output_root", "error",
        )
    })
    data["pending_decision"] = json.loads(header["pending_decision_json"])
    data["messages"] = [
        {
            "role": row["role"], "content": row["content"], "created_at": row["created_at"],
            "response_kind": row["response_kind"], "gaps": json.loads(row["gaps_json"]),
        }
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY position",
            (conversation_id,),
        )
    ]
    data["task"] = None
    data["archived_tasks"] = []
    for row in connection.execute(
        """SELECT * FROM tasks WHERE conversation_id=?
           ORDER BY archive_position IS NULL, archive_position, id""", (conversation_id,),
    ):
        task = json.loads(row["state_json"])
        task.update({
            "id": row["id"], "stage": row["stage"],
            "baseline": json.loads(row["baseline_json"]),
            "result": json.loads(row["result_json"]),
            "calls": [], "pending_specs": {}, "analysis_runs": [], "artifacts": [],
        })
        receipts = []
        for call in connection.execute(
            "SELECT * FROM calls WHERE task_id=? ORDER BY position", (row["id"],),
        ):
            task["calls"].append({
                "id": call["id"], "node_id": call["node_id"], "tool": call["capability"],
                "tool_version": call["capability_version"],
                "baseline_version": call["baseline_version"], "status": call["status"],
                "started_at": call["started_at"], "finished_at": call["finished_at"],
                "inputs": json.loads(call["inputs_json"]),
                "output": json.loads(call["output_json"]), "error": call["error"],
            })
            if call["spec_pending"]:
                task["pending_specs"][call["id"]] = json.loads(call["spec_json"])
            if call["receipt_position"] is not None:
                receipts.append((call["receipt_position"], json.loads(call["receipt_json"])))
        task["analysis_runs"] = [receipt for _, receipt in sorted(receipts)]
        task["artifacts"] = [
            json.loads(artifact["descriptor_json"])
            for artifact in connection.execute(
                """SELECT descriptor_json FROM artifacts
                   WHERE task_id=? AND slot='task' ORDER BY position""", (row["id"],),
            )
        ]
        if row["baseline_version"] != task["baseline"]["version"]:
            raise ValueError("Stored baseline version disagrees with its validated payload")
        if row["id"] == header["current_task_id"]:
            data["task"] = task
        elif row["archive_position"] is not None:
            data["archived_tasks"].append(task)
        else:
            raise ValueError("Stored task is neither current nor archived")
    record = Conversation.model_validate(data)
    if record.id != conversation_id:
        raise ValueError("Conversation identity mismatch")
    return record
```

- [ ] **Step 6: Implement transaction ownership and validated artifact publication**

Create `sqlite_store.py`. Keep it independent of `.store` to avoid the final re-export creating an import cycle.

```python
import hashlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..models import JOB_ID
from ..runtime.evidence import load_agent_run, verify_evidence
from ..runtime.observations import OutputArtifact
from .contracts import Conversation
from .intake import safe_directory
from .sqlite_codec import Row, canonical, decode, encode
from .sqlite_cutover import prepare_cutover
from .sqlite_schema import connect_database, migrate_database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteConversationStore:
    def __init__(self, root: Path, *, require_empty: bool = False):
        safe_directory(root)
        self.root = root.resolve()
        self.backup_path = prepare_cutover(self.root, require_empty=require_empty)
        self.database_path = self.root / "conversations.sqlite3"
        migrate_database(self.database_path)
        self._local = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            name = "nested_" + uuid4().hex
            existing.execute(f"SAVEPOINT {name}")
            try:
                yield
                existing.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                existing.execute(f"ROLLBACK TO SAVEPOINT {name}")
                existing.execute(f"RELEASE SAVEPOINT {name}")
                raise
            return
        connection = connect_database(self.database_path)
        self._local.connection = connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise OSError(f"Conversation database transaction failed: {exc}") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._local.connection = None
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN")
            yield connection
        except sqlite3.Error as exc:
            raise OSError(f"Conversation database read failed: {exc}") from exc
        finally:
            connection.rollback()
            connection.close()

    def directory(self, conversation_id: str) -> Path:
        if not JOB_ID.fullmatch(conversation_id):
            raise KeyError("Unknown conversation")
        path = self.root / conversation_id
        safe_directory(path)
        if path.is_symlink() or not path.is_dir():
            raise KeyError("Unknown conversation")
        return path

    def default_task_root(self, conversation_id: str) -> Path:
        return self.directory(conversation_id) / "tasks"

    def task_directory(
        self, conversation_id: str, task_id: str, output_root: str | None = None,
        *, create: bool = False,
    ) -> Path:
        if not JOB_ID.fullmatch(task_id):
            raise ValueError("Invalid task ID")
        internal = self.default_task_root(conversation_id) / task_id
        path = Path(output_root) / f"flight-agent-{task_id}" if output_root else internal
        safe_directory(path)
        if create:
            path.mkdir(parents=True, exist_ok=False)
        elif not path.is_dir():
            raise ValueError("Task result directory is missing")
        return path

    def create(self) -> Conversation:
        stamp = _now()
        record = Conversation(id=uuid4().hex, created_at=stamp, updated_at=stamp)
        directory = self.root / record.id
        directory.mkdir()
        try:
            self.save(record)
        except BaseException:
            directory.rmdir()
            raise
        return record

    def get(self, conversation_id: str) -> Conversation:
        self.directory(conversation_id)
        with self._read() as connection:
            return decode(connection, conversation_id)

    def recent(self) -> list[Conversation]:
        with self._read() as connection:
            identifiers = [
                row[0] for row in connection.execute(
                    "SELECT id FROM conversations ORDER BY updated_at DESC, id ASC LIMIT 20"
                )
            ]
            return [decode(connection, identifier) for identifier in identifiers]

    @staticmethod
    def _upsert(connection: sqlite3.Connection, table: str, row: Row) -> None:
        columns = list(row)
        keys = (
            ("conversation_id", "position") if table == "messages"
            else ("task_id", "slot", "position") if table == "artifacts"
            else ("id",)
        )
        where = " AND ".join(f"{key}=?" for key in keys)
        updates = ",".join(f"{name}=?" for name in columns)
        cursor = connection.execute(
            f"UPDATE {table} SET {updates} WHERE {where}",
            (*row.values(), *(row[key] for key in keys)),
        )
        if cursor.rowcount:
            return
        marks = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", tuple(row.values()),
        )

    def _check_artifacts(
        self, connection: sqlite3.Connection, record: Conversation,
        rows: dict[str, list[Row]],
    ) -> None:
        tasks = {task.id: task for task in [
            *record.archived_tasks, *([record.task] if record.task else []),
        ]}
        for row in rows["artifacts"]:
            task = tasks[str(row["task_id"])]
            previous = connection.execute(
                "SELECT * FROM artifacts WHERE task_id=? AND slot=? AND position=?",
                (row["task_id"], row["slot"], row["position"]),
            ).fetchone()
            if previous is not None and dict(previous) == row:
                continue
            directory = self.task_directory(record.id, task.id, task.output_root)
            if str(directory) != task.result_directory:
                raise ValueError("Task result directory does not match its persisted identity")
            verify_evidence(directory, OutputArtifact.model_validate_json(
                str(row["descriptor_json"])
            ))
        for task in tasks.values():
            runs = {run.id: run for run in task.analysis_runs}
            for call in task.calls:
                if call.status != "completed":
                    continue
                previous = connection.execute(
                    "SELECT * FROM calls WHERE id=?", (call.id,)
                ).fetchone()
                run = runs.get(call.id)
                if run is None:
                    raise ValueError("Completed call lacks its invocation receipt")
                receipt_json = canonical(run.model_dump(mode="json"))
                if previous is not None and previous["receipt_json"] != "null":
                    if previous["receipt_json"] != receipt_json:
                        raise ValueError("Invocation receipt metadata is immutable")
                node = next((node for node in task.nodes if node.id == call.node_id), None)
                if (
                    node is None or node.status != "completed"
                    or run.id != call.id or run.spec.inputs != call.inputs
                    or run.spec.capability != call.tool
                    or run.spec.capability_version != call.tool_version
                    or call.baseline_version != task.baseline.version
                    or call.started_at != run.started_at
                    or call.finished_at != run.finished_at
                    or call.output != run.output.model_dump(mode="json")
                    or task.outputs.get(node.id) != run.output
                    or call.id in task.pending_specs
                    or previous is not None and previous["spec_json"] != canonical(
                        run.spec.model_dump(mode="json")
                    )
                ):
                    raise ValueError("Terminal call, node, output, and receipt disagree")
                if previous is not None and previous["status"] == "completed":
                    continue
                directory = self.task_directory(record.id, task.id, task.output_root)
                if load_agent_run(directory, call.id, run.spec) != run:
                    raise ValueError("Completed call lacks its verified disk receipt")
```

This makes the invariant precise: **new or changed artifact claims and newly terminal calls are verified before commit**. Previously verified ledger entries remain auditable after external disk damage; `get()` can still return their metadata so the runtime can record failure. Existing `_verify_saved()`, receipt loading, and authenticated download continue to verify bytes before use. Export in Task 4 also verifies every indexed descriptor. Never “repair” missing completed evidence by silently deleting its ledger entry or redispatching its operation.

- [ ] **Step 7: Save changed rows and append audit transitions atomically**

Append these methods to `SQLiteConversationStore`:

```python
    def save(self, record: Conversation) -> None:
        self.directory(record.id)
        validated = Conversation.model_validate(record.model_dump(mode="json"))
        if len(canonical(validated.model_dump(mode="json")).encode("utf-8")) > 8_000_000:
            raise ValueError("Conversation exceeds 8 MB; existing state was not replaced")
        validated.updated_at = _now()
        with self.transaction():
            connection = self._local.connection
            previous_artifacts = [
                dict(row) for row in connection.execute(
                    """SELECT artifacts.* FROM artifacts JOIN tasks ON tasks.id=artifacts.task_id
                       WHERE tasks.conversation_id=?""", (record.id,),
                )
            ]
            rows = encode(validated, previous_artifacts)
            self._check_artifacts(connection, validated, rows)
            old_messages = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY position",
                    (record.id,),
                )
            ]
            if rows["messages"][:len(old_messages)] != old_messages:
                raise ValueError("Recorded messages are append-only")
            old_tasks = {
                row["id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM tasks WHERE conversation_id=?", (record.id,),
                )
            }
            if not set(old_tasks) <= {row["id"] for row in rows["tasks"]}:
                raise ValueError("Archived tasks cannot be dropped")
            old_calls = [
                dict(row) for row in connection.execute(
                    """SELECT calls.* FROM calls JOIN tasks ON tasks.id=calls.task_id
                       WHERE tasks.conversation_id=? ORDER BY calls.task_id, calls.position""",
                    (record.id,),
                )
            ]
            if not {row["id"] for row in old_calls} <= {row["id"] for row in rows["calls"]}:
                raise ValueError("Recorded calls cannot be dropped")
            for row in rows["tasks"]:
                previous = old_tasks.get(str(row["id"]))
                if previous is not None:
                    row["created_at"] = previous["created_at"]
            for table in ("conversations", "tasks", "messages", "calls", "artifacts"):
                for row in rows[table]:
                    keys = (
                        ("conversation_id", "position") if table == "messages"
                        else ("task_id", "slot", "position") if table == "artifacts"
                        else ("id",)
                    )
                    where = " AND ".join(f"{key}=?" for key in keys)
                    previous = connection.execute(
                        f"SELECT * FROM {table} WHERE {where}", tuple(row[key] for key in keys),
                    ).fetchone()
                    if table in ("tasks", "calls") and previous is not None:
                        owner = "conversation_id" if table == "tasks" else "task_id"
                        if previous[owner] != row[owner]:
                            raise ValueError("Stored ledger identity belongs to another owner")
                    if previous is not None and dict(previous) == row:
                        continue
                    self._upsert(connection, table, row)
            for row in rows["tasks"]:
                task_id = str(row["id"])
                old_task = old_tasks.get(task_id)
                before = self._state_digest(
                    old_task, [call for call in old_calls if call["task_id"] == task_id],
                )
                after = self._state_digest(
                    row, [call for call in rows["calls"] if call["task_id"] == task_id],
                )
                if before != after:
                    connection.execute(
                        """INSERT INTO task_events(task_id, kind, created_at, payload_json)
                           VALUES (?, 'state_transition', ?, ?)""",
                        (task_id, validated.updated_at, canonical({
                            "from": before, "to": after, "stage": row["stage"],
                            "status": row["status"],
                        })),
                    )
        record.updated_at = validated.updated_at

    @staticmethod
    def _state_digest(task: Row | None, calls: list[Row]) -> str | None:
        if task is None:
            return None
        state = {
            "task": {key: value for key, value in task.items() if key != "updated_at"},
            "calls": sorted(calls, key=lambda row: int(row["position"] or 0)),
        }
        return hashlib.sha256(canonical(state).encode("utf-8")).hexdigest()
```

SQL identifiers come only from the fixed encoder dictionaries and fixed table names, never request input; all values are bound parameters. `save()` always enters the repository transaction; nested calls use savepoints, never independently commit their parent's unit of work. Rollback restores the **database**, not mutable Python objects: after a failed unit of work discard/reload the conversation and reconstruct its budget account before continuing.

Avoid rewriting unchanged task headers just because the conversation timestamp changed. Before row comparison in the upsert loop, add:

```python
                    if table == "tasks" and previous is not None:
                        without_stamp = {key: value for key, value in row.items()
                                         if key != "updated_at"}
                        old_without_stamp = {
                            key: previous[key] for key in previous.keys() if key != "updated_at"
                        }
                        if without_stamp == old_without_stamp:
                            row["updated_at"] = previous["updated_at"]
```

The task-event digest includes baseline, node, budget, and call state, so confirmation, dispatch, terminal acceptance, and recovery changes have append-only audit facts. A duplicate save does not invent a transition.

- [ ] **Step 8: Run GREEN, formatting/type checks, and commit**

Run:

```bash
python -m pytest tests/test_sqlite_store.py tests/test_sqlite_schema.py tests/test_sqlite_cutover.py -q
python -m ruff check src/flight_agent/verification/sqlite_*.py src/flight_agent/verification/conversation_repository.py tests/test_sqlite_*.py
python -m mypy src/flight_agent/verification/sqlite_schema.py src/flight_agent/verification/sqlite_cutover.py src/flight_agent/verification/sqlite_codec.py src/flight_agent/verification/sqlite_store.py
```

Expected: all selected tests PASS, Ruff reports `All checks passed!`, and mypy reports success. Keep SQLite row typing explicit; do not suppress a domain type error with `# type: ignore`.

```bash
git add src/flight_agent/verification/conversation_repository.py src/flight_agent/verification/sqlite_codec.py src/flight_agent/verification/sqlite_store.py tests/test_sqlite_store.py
git commit -m "feat: persist conversation metadata in transactional SQLite rows"
```

### Task 4: Restart interruption and deterministic support export

**Files:**
- Modify: `src/flight_agent/verification/sqlite_store.py`
- Modify: `src/flight_agent/verification/conversation_repository.py`
- Test: `tests/test_sqlite_store.py`
- Read, unchanged: `src/flight_agent/verification/agent_runtime.py:691-747`

**Interfaces:**
- Produces: `SQLiteConversationStore.export(conversation_id: str) -> bytes`
- Produces: `ConversationRepository.export(conversation_id: str) -> bytes`
- Produces: `SQLiteConversationStore._recover_startup() -> None`, called once from construction after migration.
- Consumes: `Conversation.restore_turn_origin()`, `PendingDecision`, current unknown-call reconciliation in `AgentExecutor._recover()`.
- Export envelope: `{"schema_version": 1, "conversation": <current validated domain JSON>, "artifact_index": <ordered metadata rows>, "task_events": <ordered audit rows>}`.

- [ ] **Step 1: Add failing restart, checkpoint, and export tests**

Append to `tests/test_sqlite_store.py`:

```python
def test_restart_interrupts_running_calls_even_under_nonrunning_header(tmp_path):
    from flight_agent.runtime.agent_contracts import InvocationSpec
    from flight_agent.verification.contracts import ToolCall

    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    task = record.task
    task.nodes[0].status = "running"
    call = ToolCall(
        id="1" * 32, node_id="compute", tool="offline_rmse", tool_version="1",
        baseline_version=1, started_at=now(),
        inputs={"observed": [0, 2], "reference": [0, 0]},
    )
    task.calls.append(call)
    task.pending_specs[call.id] = InvocationSpec(
        capability="offline_rmse", capability_version="1", inputs=call.inputs
    )
    record.status = "failed"
    store.save(record)
    saved = SQLiteConversationStore(store.root).get(record.id)
    assert saved.status == "interrupted"
    assert saved.task.calls[0].status == "interrupted"
    assert saved.task.calls[0].id == call.id
    assert saved.task.calls[0].inputs == call.inputs
    assert saved.task.pending_specs == task.pending_specs
    assert saved.task.budget_state == task.budget_state
    assert saved.task.nodes[0].status == "interrupted"
    assert SQLiteConversationStore(store.root).get(record.id) == saved


def test_planning_restart_restores_followup_origin_without_touching_baseline(tmp_path):
    from flight_agent.verification.contracts import ConversationState

    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    record.turn_origin = ConversationState(status="waiting_for_user")
    record.status = "planning"
    store.save(record)
    saved = SQLiteConversationStore(store.root).get(record.id)
    assert saved.status == "waiting_for_user"
    assert saved.turn_origin is None
    assert saved.task.baseline == record.task.baseline
    assert saved.messages[-1].role == "assistant"
    assert len(SQLiteConversationStore(store.root).get(record.id).messages) == 1


def test_export_is_stable_complete_and_does_not_change_live_state(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    (directory / "metric.json").write_text('{"rmse":1}')
    record.task.artifacts.append(OutputArtifact(
        **describe_evidence(directory, "metric.json").model_dump()
    ))
    store.save(record)
    before = store.get(record.id)
    first = store.export(record.id)
    assert first == store.export(record.id)
    assert first.endswith(b"\n")
    payload = json.loads(first)
    assert payload["schema_version"] == 1
    assert payload["conversation"] == before.model_dump(mode="json")
    assert payload["artifact_index"][0]["relative_path"] == "metric.json"
    assert payload["task_events"][0]["kind"] == "state_transition"
    assert store.get(record.id) == before
    (directory / "metric.json").write_text('{"rmse":2}')
    with pytest.raises(ValueError, match="integrity"):
        store.export(record.id)
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_sqlite_store.py -k 'restart or export' -q`

Expected: restart assertions FAIL because startup has no recovery; export test FAILS with `AttributeError` for `export`.

- [ ] **Step 3: Recover once, atomically, before serving requests**

Import `ConversationMessage` and `PendingDecision` from `.contracts`, and `DatabaseStartupError` from `.sqlite_schema`, in `sqlite_store.py`. At the end of `__init__()`, append:

```python
        try:
            self._recover_startup()
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise DatabaseStartupError(f"SQLite startup recovery failed: {exc}") from exc
```

Add:

```python
    def _recover_startup(self) -> None:
        with self.transaction():
            connection = self._local.connection
            identifiers = [
                row[0] for row in connection.execute(
                    """SELECT DISTINCT conversations.id FROM conversations
                       LEFT JOIN tasks ON tasks.conversation_id=conversations.id
                       LEFT JOIN calls ON calls.task_id=tasks.id
                       WHERE conversations.status IN ('planning','running')
                          OR calls.status='running'
                       ORDER BY conversations.id"""
                )
            ]
            for identifier in identifiers:
                record = decode(connection, identifier)
                tasks = [
                    *record.archived_tasks, *([record.task] if record.task else []),
                ]
                running = [call for task in tasks for call in task.calls
                           if call.status == "running"]
                if record.status == "planning" and not running and record.restore_turn_origin():
                    record.messages.append(ConversationMessage(
                        role="assistant",
                        content="服务在处理追问时重启；原任务状态已保留，请重新发送问题或续接任务。",
                        created_at=_now(),
                    ))
                    self.save(record)
                    continue
                error = "Service restarted; resume saved work without resubmitting a worker run."
                for task in tasks:
                    interrupted_nodes = set()
                    for call in task.calls:
                        if call.status == "running":
                            call.status = "interrupted"
                            call.error = error
                            call.finished_at = _now()
                            interrupted_nodes.add(call.node_id)
                    for node in task.nodes:
                        if node.status == "running" or node.id in interrupted_nodes:
                            node.status = "interrupted"
                            node.error = error
                    if interrupted_nodes:
                        task.stage = "interrupted"
                record.status = "interrupted"
                record.error = error
                record.pending_decision = PendingDecision(
                    kind="resume" if record.task and record.task.baseline.confirmed else "clarify",
                    message=error,
                )
                self.save(record)
```

Mark **all** unresolved dispatched calls interrupted, including ones whose conversation header was already failed, and preserve call IDs, pending specs, checkpoints, baseline hashes, decisions, and charged budget. This is conservative for capabilities with reconciliation: interruption does not turn them into retryable failures, erase the original identity, or disable `CapabilityRegistry.reconcile()`. Reconciliation remains an explicit resume path through the existing executor, never a startup invocation of user code. No capability in this stage gets a new automatic reconciliation policy.

- [ ] **Step 4: Export from one read snapshot with explicit ordering**

Append the support-export signature to `ConversationRepository`:

```python
    def export(self, conversation_id: str) -> bytes: ...
```

Add:

```python
    def export(self, conversation_id: str) -> bytes:
        self.directory(conversation_id)
        with self._read() as connection:
            record = decode(connection, conversation_id)
            tasks = {task.id: task for task in [
                *record.archived_tasks, *([record.task] if record.task else []),
            ]}
            artifacts = [
                dict(row) for row in connection.execute(
                    """SELECT artifacts.* FROM artifacts JOIN tasks ON tasks.id=artifacts.task_id
                       WHERE tasks.conversation_id=?
                       ORDER BY artifacts.task_id, artifacts.slot, artifacts.position""",
                    (conversation_id,),
                )
            ]
            for row in artifacts:
                task = tasks[str(row["task_id"])]
                root = self.task_directory(record.id, task.id, task.output_root)
                if str(root) != task.result_directory:
                    raise ValueError("Task result directory does not match its persisted identity")
                verify_evidence(root, OutputArtifact.model_validate_json(row["descriptor_json"]))
            events = [
                dict(row) for row in connection.execute(
                    """SELECT task_events.* FROM task_events
                       JOIN tasks ON tasks.id=task_events.task_id
                       WHERE tasks.conversation_id=? ORDER BY task_events.sequence""",
                    (conversation_id,),
                )
            ]
            payload = {
                "schema_version": 1, "conversation": record.model_dump(mode="json"),
                "artifact_index": artifacts, "task_events": events,
            }
            return (canonical(payload) + "\n").encode("utf-8")
```

Determinism means repeated export of the **same committed state** produces byte-identical UTF-8 output: no export timestamp, random export ID, filesystem enumeration order, or database page layout. It does not mean two independently created conversations share IDs/timestamps. Export contains metadata and domain state, not evidence bytes, and must be treated as sensitive support material.

- [ ] **Step 5: Run GREEN and commit**

Run: `python -m pytest tests/test_sqlite_store.py tests/test_sqlite_schema.py tests/test_sqlite_cutover.py -q`

Expected: all tests PASS; opening a second time does not append another interruption message/event or change the interrupted record.

```bash
git add src/flight_agent/verification/sqlite_store.py src/flight_agent/verification/conversation_repository.py tests/test_sqlite_store.py
git commit -m "feat: recover interrupted SQLite conversations and export deterministic state"
```

### Task 5: Activate SQLite and make runtime terminal acceptance atomic

**Files:**
- Modify: `src/flight_agent/verification/store.py:1-21,90-201`
- Modify: `src/flight_agent/verification/agent_runtime.py:45-60,155-190,237-246,337-346,668-867`
- Modify: `src/flight_agent/verification/coordinator.py:12-28`
- Modify: `src/flight_agent/web.py:35-76`
- Create: `tests/test_sqlite_runtime.py`
- Modify: `tests/test_agent_history.py:1-94,134-148`
- Modify: `tests/test_agent_recovery.py:125-136,291-329`
- Modify: `tests/test_legacy_cleanup.py:447-484`
- Modify: `tests/test_conversation_bounds.py:1-20`
- Read, unchanged: `src/flight_agent/runtime/budget.py`
- Read, unchanged: `src/flight_agent/verification/routes.py`

**Interfaces:**
- Consumes: `ConversationRepository.transaction()`, all existing repository methods.
- Changes: `AgentExecutor._accept(self, conversation: Conversation, task: AgentTask, node: AgentExecutionNode, call: ToolCall, run: InvocationRun, budget: BudgetAccount) -> None`.
- Preserves: `persist_running_call(conversation: Conversation, task: AgentTask, node: AgentExecutionNode, spec: InvocationSpec, *, store: ConversationRepository) -> ToolCall`.
- Preserves: `build_context(conversation: Conversation, task: AgentTask, node: AgentExecutionNode, call: ToolCall, budget: BudgetAccount, *, store: ConversationRepository) -> AgentContext`.
- Preserves import compatibility: `verification.store.ConversationStore` names **the SQLite class**, not a JSON implementation or runtime type switch.
- Preserves: `WorkerRunStore`, `request_digest()`, `now()`, Worker `status.json`/`request.json`, all evidence/checkpoint JSON.

- [ ] **Step 1: Write failing pre-dispatch and terminal-atomicity tests**

```python
# tests/test_sqlite_runtime.py
import sqlite3

import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from tests.test_agent_runtime import confirm, propose, setup


def test_external_operation_sees_committed_sqlite_dispatch_and_charged_budget(tmp_path):
    seen = []

    def operation(context, inputs):
        with sqlite3.connect(store.database_path) as reader:
            row = reader.execute(
                "SELECT id,status,spec_pending FROM calls WHERE id=?", (context.call_id,)
            ).fetchone()
            assert row == (context.call_id, "running", 1)
            assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
        saved = store.get(pending.id)
        assert saved.task.budget_state.calls == 1
        assert saved.task.budget_state.rounds == 1
        assert saved.task.baseline.confirmed
        seen.append(context.call_id)
        return CapabilityOutcome()

    executor, store, record, _ = setup(tmp_path, operation=operation)
    assert isinstance(store, SQLiteConversationStore)
    pending = propose(executor, record)
    done = confirm(executor, pending)
    assert seen == [done.task.calls[0].id]


def test_terminal_state_and_progress_are_one_commit(tmp_path, monkeypatch):
    executor, store, record, calls = setup(tmp_path)
    pending = propose(executor, record)
    progress = executor._progress
    observed = []

    def inspect_before_progress(task, run, budget):
        assert task.calls[0].status == "completed"
        with sqlite3.connect(store.database_path) as outside:
            assert outside.execute(
                "SELECT status FROM calls WHERE id=?", (run.id,)
            ).fetchone()[0] == "running"
        observed.append(run.id)
        progress(task, run, budget)
        persisted = store.get(pending.id)
        assert persisted.task.calls[0].status == "completed"
        assert run.id in persisted.task.progress_calls
        with sqlite3.connect(store.database_path) as outside:
            assert outside.execute(
                "SELECT status FROM calls WHERE id=?", (run.id,)
            ).fetchone()[0] == "running"

    monkeypatch.setattr(executor, "_progress", inspect_before_progress)
    done = confirm(executor, pending)
    assert done.status == "completed", done.error
    assert observed == calls
    saved = store.get(done.id)
    assert saved.task.calls[0].status == "completed"
    assert saved.task.analysis_runs[0].id in saved.task.progress_calls
    assert saved.task.budget_state.no_progress == 0


def test_crash_in_acceptance_rolls_back_node_receipt_and_progress(tmp_path, monkeypatch):
    executor, store, record, calls = setup(tmp_path)
    pending = propose(executor, record)
    progress = executor._progress

    def crash(task, run, budget):
        progress(task, run, budget)
        raise KeyboardInterrupt("after progress save before transaction commit")

    monkeypatch.setattr(executor, "_progress", crash)
    with pytest.raises(KeyboardInterrupt):
        confirm(executor, pending)
    saved = store.get(pending.id)
    assert saved.task.calls[0].status == "running"
    assert saved.task.nodes[0].status == "running"
    assert saved.task.analysis_runs == []
    assert saved.task.progress_calls == []
    assert saved.task.budget_state.calls == 1
    restarted_store = SQLiteConversationStore(store.root)
    recovery = type(executor)(restarted_store, executor.tools, executor.resources)
    recovered = recovery.resume(pending.id)
    assert recovered.status == "completed", recovered.error
    assert len(calls) == 1


def test_handled_acceptance_failure_does_not_resave_rolled_back_progress(tmp_path, monkeypatch):
    executor, store, record, calls = setup(tmp_path)
    pending = propose(executor, record)
    progress = executor._progress

    def fail(task, run, budget):
        progress(task, run, budget)
        raise RuntimeError("acceptance acknowledgement failed")

    monkeypatch.setattr(executor, "_progress", fail)
    interrupted = confirm(executor, pending)
    assert interrupted.status == "interrupted"
    saved = store.get(pending.id)
    assert saved.task.calls[0].status == "interrupted"
    assert saved.task.analysis_runs == []
    assert saved.task.progress_calls == []
    assert saved.task.budget_state.calls == 1
    recovery = type(executor)(
        SQLiteConversationStore(store.root), executor.tools, executor.resources,
    )
    recovered = recovery.resume(pending.id)
    assert recovered.status == "completed", recovered.error
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["future", "corrupt"])
def test_web_startup_refuses_unusable_database_without_json_fallback(tmp_path, damage):
    from flight_agent.verification.sqlite_schema import DatabaseStartupError
    from flight_agent.web import create_web_app

    store = SQLiteConversationStore(tmp_path / "runs" / "verification", require_empty=True)
    if damage == "future":
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("PRAGMA user_version=999")
    else:
        store.database_path.write_bytes(b"not a SQLite database")
    with pytest.raises(DatabaseStartupError):
        create_web_app(tmp_path)
    assert not list(store.root.rglob("conversation.json"))


def test_process_exit_after_dispatch_keeps_original_unretryable_call(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.agent_runtime import AgentExecutor
    from flight_agent.verification.resources import ResourceRegistry
    from tests.agent_helpers import register_rmse

    script = """
import os
from pathlib import Path
from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from tests.agent_helpers import analysis_plan_data, register_rmse

root = Path(os.environ["FLIGHT_TEST_STATE"])
store = SQLiteConversationStore(root, require_empty=True)
tools = CapabilityRegistry(agent_mode=True)
def stop(context, inputs):
    os._exit(73)
register_rmse(tools, [], operation=stop)
executor = AgentExecutor(store, tools, ResourceRegistry())
record = executor.propose(store.create(), AgentPlan.model_validate(analysis_plan_data()))
executor.confirm(record.id, 1, trusted=True)
"""
    environment = dict(os.environ)
    environment["FLIGHT_TEST_STATE"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = os.pathsep.join([str(Path.cwd() / "src"), str(Path.cwd())])
    process = subprocess.run(
        [sys.executable, "-c", script], env=environment, timeout=15, check=False,
        capture_output=True, text=True,
    )
    assert process.returncode == 73, process.stderr
    store = SQLiteConversationStore(tmp_path / "state")
    record = store.recent()[0]
    original = record.task.calls[0].id
    assert record.status == "interrupted"
    assert record.task.calls[0].status == "interrupted"
    assert record.task.budget_state.calls == 1
    assert original in record.task.pending_specs
    operations = []
    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, operations)
    executor = AgentExecutor(store, tools, ResourceRegistry())
    with pytest.raises(ValueError, match="unknown|unresolved"):
        executor.retry(record.id, 1, trusted=True)
    blocked = executor.resume(record.id)
    assert blocked.status == "interrupted"
    assert blocked.task.calls[0].id == original
    assert operations == []


def test_failed_message_insert_rolls_back_header_transition(tmp_path):
    _executor, store, record, _ = setup(tmp_path)
    before = store.get(record.id)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_user_message BEFORE INSERT ON messages
            WHEN NEW.role='user'
            BEGIN SELECT RAISE(ABORT, 'injected message failure'); END
        """)
    from flight_agent.verification.contracts import ConversationMessage
    from flight_agent.verification.store import now

    record.status = "planning"
    record.messages.append(ConversationMessage(
        role="user", content="Analyze", created_at=now(),
    ))
    with pytest.raises(OSError, match="injected message failure"):
        store.save(record)
    assert store.get(record.id) == before
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
```

The subprocess test deliberately bypasses Python cleanup with `os._exit`; it is not a request to kill unrelated processes or start a long-lived background service. The SQL trigger fails **after** the header update and verifies actual SQLite rollback, not just validation failure before any SQL.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_sqlite_runtime.py -q`

Expected: the store-type/`database_path` assertions FAIL while the import alias still names the JSON class; terminal-progress transaction assertions are not yet satisfied.

- [ ] **Step 3: Replace the production store, preserving the Worker boundary**

In `verification/store.py`, delete only the entire `ConversationStore` class (currently lines 90–201), its `uuid4` import, conversation-only contract imports, `safe_directory`, and runtime imports of `classify_record`/`normalize_v2_record`. Keep `WorkerRunStore`, its `threading.RLock`, `AnalysisRunRequest`, `RunRecord`, `write_json`, `JOB_ID`, digest utility, and `now()`.

Add the compatibility re-export at module import scope:

```python
from .sqlite_store import SQLiteConversationStore as ConversationStore
```

In `web.py`, replace the store import and default construction:

```python
from .verification.sqlite_store import SQLiteConversationStore
```

```python
        conversations = SQLiteConversationStore(root / "runs" / "verification")
```

Do not construct a second store when `engineering_runtime` is supplied. Schema migration, backup, or recovery errors propagate out of `create_web_app()` before it returns an app; do not swallow them as an unavailable capability or fall back to JSON.

In `agent_runtime.py` and `coordinator.py`, import `ConversationRepository` from `.conversation_repository`, keep `now` imported from `.store`, and replace every `store: ConversationStore` annotation with `store: ConversationRepository`. No annotation changes the runtime object or introduces a scheduling abstraction.

Leave `EngineeringRuntime.queue_message()`'s one-save behavior in place: the new `save()` writes the appended user row and resulting status/roots/pending decision in one transaction, including `_wait()` and unresolved-call responses. Do not open a write transaction across resource discovery, model calls, directory parsing, user confirmation delays, or capability invocation.

- [ ] **Step 4: Commit terminal acceptance and budget progress together**

Replace `_accept()` with:

```python
    def _accept(
        self,
        conversation: Conversation,
        task: AgentTask,
        node: AgentExecutionNode,
        call: ToolCall,
        run: InvocationRun,
        budget: BudgetAccount,
    ) -> None:
        exhausted: BudgetExhausted | None = None
        previous = task.model_copy(deep=True)
        previous_budget = budget.state.model_copy(deep=True)
        previous_stamp = conversation.updated_at
        try:
            with self.store.transaction():
                accept_verified_run(task, node, call, run)
                receipt = OutputArtifact(
                    **describe_evidence(
                        self._directory(conversation, task), agent_receipt_name(run.id)
                    ).model_dump()
                )
                for artifact in [*run.artifacts, *run.output.artifacts, receipt]:
                    if artifact not in task.artifacts:
                        task.artifacts.append(artifact)
                try:
                    self._progress(task, run, budget)
                except BudgetExhausted as exc:
                    exhausted = exc
                self.store.save(conversation)
        except BaseException:
            original_node = next(item for item in previous.nodes if item.id == node.id)
            original_call = next(item for item in previous.calls if item.id == call.id)
            for name in AgentExecutionNode.model_fields:
                setattr(node, name, getattr(original_node, name))
            for name in ToolCall.model_fields:
                setattr(call, name, getattr(original_call, name))
            for name in AgentTask.model_fields:
                setattr(task, name, getattr(previous, name))
            task.nodes = [node if item.id == node.id else item for item in task.nodes]
            task.calls = [call if item.id == call.id else item for item in task.calls]
            budget.state = previous_budget
            conversation.updated_at = previous_stamp
            raise
        if exhausted is not None:
            raise exhausted
```

Change both call sites in `_dispatch()` and `_recover()` to:

```python
            self._accept(conversation, task, node, call, run, budget)
```

Remove the old `self._progress(task, run, budget)` immediately after each acceptance/error-handling block. Keep `_run()`'s existing recovery loop over `task.analysis_runs`; it is idempotent and remains useful for existing valid state.

Because a budget limit can now be raised **after successful terminal commit**, add this branch before the existing `except (OSError, ValueError, RuntimeError)` handlers in both `_dispatch()` and `_recover()`:

```python
        except BudgetExhausted:
            raise
```

Its purpose is explicit: elapsed/no-progress exhaustion after acceptance must reach `_run()` without resetting a completed call to interrupted, removing its receipt, or restoring its pending spec. A limit before any new dispatch still propagates through the existing charge/check paths and does not create a call.

The rollback handler restores the already-validated Python state as well as relying on SQLite rollback. This matters because the existing `_dispatch()` failure handler subsequently saves the interrupted call: it must not accidentally persist progress counters from a rolled-back terminal transition. The `BaseException` handler is cleanup-and-reraise only, never a successful fallback.

`BudgetAccount._save()` invokes the checkpoint while `self.state` still has its previous value. The checkpoint closure in `_run()` assigns `task.budget_state = state` **before** `store.save()`, so the final `save()` inside `_accept()` must persist `task.budget_state`; never copy the stale `budget.state` into the task from inside the callback.

Do not move external invocation inside `store.transaction()`. `persist_running_call()` still saves and commits the running call/spec before invoking user code. Charge saves before dispatch remain conservative: interruption after charging but before dispatch never refunds or resets the budget.

- [ ] **Step 5: Replace only tests that require the removed JSON runtime**

Replace `test_existing_v2_envelope_normalizes_without_rewriting_persisted_history` in `tests/test_agent_history.py` with:

```python
def test_sqlite_history_round_trips_current_and_archived_v2_tasks(tmp_path):
    store = ConversationStore(tmp_path / "state")
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    archived_directory = store.task_directory(record.id, "b" * 32, create=True)
    archived = agent_task(archived_directory).model_copy(update={"id": "b" * 32})
    record.archived_tasks = [archived]
    store.save(record)
    assert ConversationStore(store.root).get(record.id) == record
    assert not list(store.root.rglob("conversation.json"))
```

The old fixture duplicated a task ID in current and archived history. Replace it with distinct identities as above; do not weaken the database primary key to accommodate an invalid fixture.

In `test_oversized_v2_state_cannot_replace_last_readable_conversation`, replace the final raw JSON read/assertion with:

```python
    assert store.get(record.id).task.schema_version == "2.0"
    assert not list(store.root.rglob("conversation.json"))
```

Remove the now-unused `import json` from `tests/test_agent_history.py`.

In `tests/test_agent_recovery.py:test_recovery_accounts_for_progress_when_crash_follows_receipt_acceptance`, change the fault wrapper to:

```python
    def crash(conversation, task, node, call, run, budget):
        accept(conversation, task, node, call, run, budget)
        if node.id == "next":
            raise KeyboardInterrupt("Crash after atomic receipt/progress acceptance")
```

Replace its pre-restart assertion with:

```python
    saved = store.get(pending.id)
    assert saved.task.budget_state.no_progress == 0
    assert saved.task.analysis_runs[-1].id in saved.task.progress_calls
```

Keep its original post-restart assertions: completed status, no extra operations, and zero no-progress count.

In `tests/test_agent_recovery.py:test_missing_completed_receipt_blocks_recovery_instead_of_recomputing`, retain the entire test. The unchanged descriptor reuse in Task 3 allows saving an interrupted header after external deletion; resume still refuses missing evidence without recomputation.

Replace the last two runtime JSON-reader tests in `tests/test_legacy_cleanup.py` with:

```python
@pytest.mark.parametrize("data", [
    {"task": {"scenario": "d1"}},
    {"task": None},
    {"schema_version": "2.0", "task": {"scenario": "d1"}},
])
def test_sqlite_store_never_deserializes_or_deletes_raw_json_records(tmp_path, data):
    raw = write_record(tmp_path / ("a" * 32), data)
    before = raw.read_bytes()
    store = ConversationStore(tmp_path)
    assert store.recent() == []
    with pytest.raises(KeyError, match="Unknown conversation"):
        store.get(raw.parent.name)
    assert raw.read_bytes() == before
    assert store.backup_path is not None
    assert (store.backup_path / raw.relative_to(tmp_path)).read_bytes() == before
```

Keep **all** earlier raw cleanup/classification/normalization tests. The explicit `cleanup-legacy` CLI remains an offline raw-record tool; normal runtime no longer imports it.

In `tests/test_conversation_bounds.py`, append:

```python
    assert not list(store.root.rglob("conversation.json"))
```

- [ ] **Step 6: Run GREEN and existing guarantee regressions**

Run:

```bash
python -m pytest tests/test_sqlite_runtime.py tests/test_sqlite_store.py tests/test_sqlite_schema.py tests/test_sqlite_cutover.py -q
python -m pytest tests/test_agent_history.py tests/test_conversation_bounds.py tests/test_legacy_cleanup.py tests/test_agent_runtime.py tests/test_agent_recovery.py tests/test_agent_evidence.py tests/test_agent_web.py tests/test_agent_delivery.py tests/test_agent_acceptance.py tests/test_verification_worker.py -m "not matlab" -q
```

Expected: both commands PASS. In particular, keep the existing dispatch-before-operation, elapsed/no-progress budget, untrusted/stale confirmation, immutable receipts, receipt-publication crash, checkpoint tamper, missing completed evidence, external output permission, and store-injection tests effective.

```bash
git add src/flight_agent/verification/store.py src/flight_agent/verification/agent_runtime.py src/flight_agent/verification/coordinator.py src/flight_agent/web.py tests/test_sqlite_runtime.py tests/test_agent_history.py tests/test_agent_recovery.py tests/test_legacy_cleanup.py tests/test_conversation_bounds.py
git commit -m "refactor: cut v2 runtime over to atomic SQLite persistence"
```

### Task 6: Operational documentation and final Stage 2 gate

**Files:**
- Create: `docs/sqlite-conversation-store.md`
- Modify: `README.md:181-198`
- Test: `tests/test_sqlite_runtime.py`
- Test: `tests/test_sqlite_store.py`
- Test: `tests/test_sqlite_schema.py`
- Read, unchanged: `src/flight_agent/verification/legacy_cleanup.py`
- Read, unchanged: `src/flight_agent/runtime/evidence.py`

**Interfaces:**
- Consumes: final `SQLiteConversationStore`, `ConversationRepository`, unchanged `create_web_app(root: Path, *, engineering_runtime: EngineeringRuntime | None = None, native_directory_picker: bool = False) -> FastAPI`.
- Produces: verified software-only Stage 2 boundary and operator instructions; no new runtime APIs or CLI commands.

- [ ] **Step 1: Run the documentation contract check RED**

Run:

```bash
python -c 'from pathlib import Path; p=Path("docs/sqlite-conversation-store.md"); assert p.is_file(), "missing SQLite operations guide"; s=p.read_text(); assert all(x in s for x in ("## First upgrade", "## Deterministic support export", "## Backups after SQLite activation", "## Rollback", "require_empty=True")); assert "docs/sqlite-conversation-store.md" in Path("README.md").read_text()'
```

Expected: `AssertionError: missing SQLite operations guide`. This final task changes documentation, not production behavior; its executable contract is documentation presence/content plus the already-red/green behavioral tests from Task 5.

- [ ] **Step 2: Document the exact operational boundary**

Create `docs/sqlite-conversation-store.md` with:

````markdown
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
````

In `README.md`, immediately after “会话记录仍保存在默认位置，旧任务和交付不会被新需求覆盖。”, add:

```markdown
v2 会话元数据和执行状态保存在
`runs/verification/conversations.sqlite3`，证据文件仍保存在任务成果目录。
首次升级前须停止旧服务；备份、硬切换、续接与回滚边界见
[SQLite 会话存储](docs/sqlite-conversation-store.md)。
```

The WAL backup distinction follows SQLite's treatment of the WAL as part of persistent database state; do not document a live main-file-only copy as safe. citeturn111160view1turn111160view2

- [ ] **Step 3: Run the documentation contract check GREEN**

Run:

```bash
python -c 'from pathlib import Path; p=Path("docs/sqlite-conversation-store.md"); assert p.is_file(), "missing SQLite operations guide"; s=p.read_text(); assert all(x in s for x in ("## First upgrade", "## Deterministic support export", "## Backups after SQLite activation", "## Rollback", "require_empty=True")); assert "docs/sqlite-conversation-store.md" in Path("README.md").read_text()'
```

Expected: exit 0 with no output.

- [ ] **Step 4: Run the complete Stage 2 software-only gate**

Run:

```bash
python -m pytest tests/test_sqlite_schema.py tests/test_sqlite_cutover.py tests/test_sqlite_store.py tests/test_sqlite_runtime.py -q
python -m pytest -m "not matlab" -q
python -m ruff check .
python -m mypy src
python -m build
git diff --check
rg -n 'conversation\.json|classify_record|normalize_v2_record' src/flight_agent/verification/store.py src/flight_agent/verification/sqlite_store.py src/flight_agent/verification/sqlite_codec.py src/flight_agent/web.py
```

Expected:

- Targeted tests and the complete non-MATLAB suite PASS; existing optional-browser skips remain skips only when their prerequisite browser is unavailable.
- Ruff: `All checks passed!`; mypy: `Success: no issues found`; build produces sdist and wheel successfully; `git diff --check` produces no output.
- Final `rg` has no matches and exits 1: there is no runtime JSON conversation reader/writer, raw-record classification, or normalization import in those active paths.
- `verification/legacy_cleanup.py` may still mention `conversation.json`; it is the explicit offline cleanup utility, not the active persistence layer.
- `WorkerRunStore` and `runtime/evidence.py` still write their own supported disk JSON. Do not use a repository-wide ban on all JSON writes.

- [ ] **Step 5: Commit the operational deliverable**

```bash
git add docs/sqlite-conversation-store.md README.md
git commit -m "docs: explain SQLite conversation cutover and recovery"
```

These are frequent implementation checkpoints on the existing Stage 2 branch. Do not amend Stage 1 or squash away a failing boundary while validating. The release includes the full Stage 2 change set; individual intermediate commits do not promise the final runtime cutover before Task 5.

## Spec coverage and completion checklist

- [ ] Seven required tables, normal identity/status columns, canonical validated JSON: Tasks 1 and 3.
- [ ] WAL, foreign keys, bounded busy timeout, versioned transactional migrations, future-schema refusal: Task 1.
- [ ] Preserved repository CRUD/path surface without a whole-conversation database blob: Task 3.
- [ ] User message plus conversation transition atomic; no database lock around model/external operations: Tasks 3 and 5, Task 6 failure injection.
- [ ] Dispatch ledger visible to an independent reader before invocation: Task 5.
- [ ] Terminal call, receipt metadata, node/output, budget/progress, and artifact rows commit together, including budget exhaustion: Task 5.
- [ ] Atomically published files verified before new artifact/terminal claims; no database evidence blobs; old evidence remains inspectable after later failures: Tasks 3 and 5.
- [ ] Startup interruption with stable identities/specs/budgets, explicit existing reconciliation, no unknown-call retry: Tasks 4–6.
- [ ] Deterministic domain export and ordered artifact/audit indexes from one read snapshot: Task 4.
- [ ] No active v2 `conversation.json` reads/writes; no startup v1 parsing or implicit JSON migration: Tasks 2 and 5.
- [ ] Verified backup or explicit-empty gate, untouched external paths, no post-activation JSON rollback: Tasks 2 and 6.
- [ ] Existing confirmation/effects/source/resource/hash/budget/receipt/recovery tests remain effective: Task 5 regression gate and Task 6 full gate.
- [ ] Existing runtime locks/admission, capability contracts, and task payload extension points remain available for later scheduler/snapshot stages, with none of those stages implemented here.
