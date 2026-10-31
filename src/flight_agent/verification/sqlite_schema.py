import fcntl
import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .sqlite_snapshot import SnapshotState, sqlite_snapshot, verify_sqlite_source


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


MIGRATIONS = (
    Migration(
        1,
        "conversation-ledger",
        (
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
        ),
    ),
)


def connect_database(
    path: Path,
    *,
    before_first_sql: Callable[[], None] | None = None,
) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        if before_first_sql is not None:
            before_first_sql()
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def _startup_identity_guard(path: Path) -> Iterator[None]:
    descriptors: list[int] = []
    try:
        parent = os.open(path.parent, os.O_RDONLY)
        descriptors.append(parent)
        fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for suffix in ("", "-journal", "-wal", "-shm"):
            entry = path.with_name(path.name + suffix)
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("SQLite startup requires regular files without symlinks")
            descriptor = os.open(
                entry,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("SQLite startup source changed while acquiring identity guard")
        yield
    except BlockingIOError as exc:
        raise ValueError("SQLite startup identity guard is already held") from exc
    finally:
        if descriptors:
            fcntl.flock(descriptors[0], fcntl.LOCK_UN)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _check_integrity(connection: sqlite3.Connection) -> None:
    try:
        results = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if results != ["ok"]:
            raise DatabaseStartupError(f"SQLite integrity check failed: {'; '.join(results)}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DatabaseStartupError("SQLite integrity check failed: invalid foreign key")
    except sqlite3.Error as exc:
        raise DatabaseStartupError(f"SQLite integrity check failed: {exc}") from exc


def _validate_database(connection: sqlite3.Connection) -> int:
    _check_integrity(connection)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    supported = MIGRATIONS[-1].version
    if version > supported:
        raise DatabaseStartupError(
            f"Database schema {version} is newer than supported schema {supported}"
        )
    has_ledger = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    applied = (
        list(
            connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
            )
        )
        if has_ledger
        else []
    )
    if any(row["version"] > supported for row in applied):
        raise DatabaseStartupError("Database migration ledger contains a newer schema")
    expected = [
        (item.version, item.name, item.sha256)
        for item in MIGRATIONS
        if item.version <= version
    ]
    if [tuple(row) for row in applied] != expected:
        raise DatabaseStartupError("Database migration ledger disagrees with schema version")
    if [item.version for item in MIGRATIONS] != list(range(1, supported + 1)):
        raise DatabaseStartupError("Database migration versions must be contiguous")
    return version


def migrate_database(
    path: Path,
    *,
    validate_aggregates: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    connection: sqlite3.Connection | None = None
    expected: SnapshotState | None = None
    try:
        with _startup_identity_guard(path):
            if path.exists() or path.is_symlink():
                with sqlite_snapshot(path) as snapshot:
                    probe = connect_database(snapshot.database)
                    try:
                        _validate_database(probe)
                        if validate_aggregates is not None:
                            try:
                                validate_aggregates(probe)
                            except (sqlite3.Error, ValueError) as exc:
                                raise DatabaseStartupError(
                                    f"SQLite aggregate validation failed: {exc}"
                                ) from exc
                    finally:
                        probe.close()
                    expected = snapshot.source_state
            else:
                if any(
                    path.with_name(path.name + suffix).exists()
                    or path.with_name(path.name + suffix).is_symlink()
                    for suffix in ("-journal", "-wal", "-shm")
                ):
                    raise DatabaseStartupError("SQLite database is missing but sidecars exist")
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            if expected is not None:
                verify_sqlite_source(path, expected)
            # sqlite3 exposes no descriptor/VFS hook; the callback repeats this check
            # after pathname open and before SQLite can recover or write.
            connection = connect_database(
                path,
                before_first_sql=(
                    None
                    if expected is None
                    else lambda: verify_sqlite_source(path, expected)
                ),
            )
            version = _validate_database(connection)
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
                        migration.version,
                        migration.name,
                        migration.sha256,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.execute(f"PRAGMA user_version={migration.version}")
            _check_integrity(connection)
            connection.commit()
    except (sqlite3.Error, OSError, ValueError) as exc:
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
