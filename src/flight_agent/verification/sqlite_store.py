import hashlib
import json
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..models import JOB_ID
from ..runtime.evidence import load_agent_run, verify_evidence
from ..runtime.observations import OutputArtifact
from .contracts import AgentTask, Conversation
from .intake import safe_directory
from .scheduling_recovery import recover_scheduling
from .sqlite_codec import Row, canonical, decode, encode
from .sqlite_cutover import prepare_cutover
from .sqlite_schema import DatabaseStartupError, connect_database, migrate_database

MAX_CONVERSATION_BYTES = 8_000_000
@dataclass(frozen=True)
class _EvidenceClaim:
    path: Path
    identity: tuple[int, int, int, int, int, int]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteConversationStore:
    def __init__(self, root: Path, *, require_empty: bool = False):
        safe_directory(root)
        self.root = root.resolve()
        self.backup_path = prepare_cutover(self.root, require_empty=require_empty)
        self.database_path = self.root / "conversations.sqlite3"
        migrate_database(
            self.database_path,
            validate_aggregates=self._validate_all_aggregates,
        )
        self._local = threading.local()
        try:
            self._recover_startup()
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise DatabaseStartupError(
                f"SQLite startup recovery failed: {exc}"
            ) from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        depth = getattr(self._local, "transaction_depth", 0)
        if depth:
            name = "nested_" + uuid4().hex
            self._local.transaction_depth = depth + 1
            self._local.savepoints.append(name)
            existing = getattr(self._local, "connection", None)
            if existing is not None:
                existing.execute(f"SAVEPOINT {name}")
            try:
                yield
                existing = getattr(self._local, "connection", None)
                if existing is not None:
                    existing.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                existing = getattr(self._local, "connection", None)
                if existing is not None:
                    existing.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    existing.execute(f"RELEASE SAVEPOINT {name}")
                raise
            finally:
                self._local.savepoints.pop()
                self._local.transaction_depth = depth
            return
        self._local.transaction_depth = 1
        self._local.savepoints = []
        self._local.connection = None
        self._local.evidence_claims = []
        try:
            yield
            connection = self._local.connection
            if connection is not None:
                self._recheck_evidence_claims(self._local.evidence_claims)
                connection.commit()
        except sqlite3.Error as exc:
            connection = self._local.connection
            if connection is not None:
                connection.rollback()
            raise OSError(f"Conversation database transaction failed: {exc}") from exc
        except BaseException:
            connection = self._local.connection
            if connection is not None:
                connection.rollback()
            raise
        finally:
            connection = self._local.connection
            self._local.connection = None
            self._local.transaction_depth = 0
            self._local.savepoints = []
            self._local.evidence_claims = []
            if connection is not None:
                connection.close()

    def _write_connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        connection = connect_database(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for name in getattr(self._local, "savepoints", []):
                connection.execute(f"SAVEPOINT {name}")
        except BaseException:
            connection.close()
            raise
        self._local.connection = connection
        return connection

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

    @staticmethod
    def _validate_all_aggregates(connection: sqlite3.Connection) -> None:
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM conversations ORDER BY id"
            )
        ]
        for identifier in identifiers:
            decode(connection, str(identifier))

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
        self,
        conversation_id: str,
        task_id: str,
        output_root: str | None = None,
        *,
        create: bool = False,
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
                row[0]
                for row in connection.execute(
                    "SELECT id FROM conversations ORDER BY updated_at DESC, id ASC LIMIT 20"
                )
            ]
            return [decode(connection, identifier) for identifier in identifiers]

    @staticmethod
    def _tasks(record: Conversation) -> list[AgentTask]:
        return [
            *record.archived_tasks,
            *([record.task] if record.task else []),
        ]

    @staticmethod
    def _serialized_size(record: Conversation) -> int:
        return len(canonical(record.model_dump(mode="json")).encode("utf-8"))

    @classmethod
    def _check_size(cls, record: Conversation) -> None:
        if cls._serialized_size(record) > MAX_CONVERSATION_BYTES:
            raise ValueError(
                "Conversation exceeds 8 MB; existing state was not replaced"
            )
        recovered = record.model_copy(deep=True)
        if not recover_scheduling(recovered):
            return
        if cls._serialized_size(recovered) > MAX_CONVERSATION_BYTES:
            raise ValueError(
                "Conversation exceeds the 8 MB limit including its startup "
                "recovery reserve; existing state was not replaced"
            )

    def _recover_startup(self) -> None:
        with self.transaction():
            connection = self._write_connection()
            identifiers = [
                row[0]
                for row in connection.execute(
                    """SELECT DISTINCT conversations.id FROM conversations
                       LEFT JOIN tasks ON tasks.conversation_id=conversations.id
                       LEFT JOIN calls ON calls.task_id=tasks.id
                       WHERE conversations.scheduling_status='queued'
                          OR conversations.status IN ('planning','running')
                          OR calls.status='running'
                       ORDER BY conversations.id"""
                )
            ]
            for conversation_id in identifiers:
                record = decode(connection, conversation_id)
                if recover_scheduling(record):
                    self.save(record)

    def export(self, conversation_id: str) -> bytes:
        self.directory(conversation_id)
        with self._read() as connection:
            record = decode(connection, conversation_id)
            tasks = {
                task.id: task for task in self._tasks(record)
            }
            artifacts = [
                dict(row)
                for row in connection.execute(
                    """SELECT artifacts.* FROM artifacts JOIN tasks ON tasks.id=artifacts.task_id
                       WHERE tasks.conversation_id=?
                       ORDER BY artifacts.task_id, artifacts.slot, artifacts.position""",
                    (conversation_id,),
                )
            ]
            roots = {}
            for task in tasks.values():
                if not JOB_ID.fullmatch(task.id):
                    raise ValueError("Invalid task ID")
                root = (
                    Path(task.output_root) / f"flight-agent-{task.id}"
                    if task.output_root
                    else self.default_task_root(record.id) / task.id
                )
                safe_directory(root)
                if str(root) != task.result_directory:
                    raise ValueError(
                        "Task result directory does not match its persisted identity"
                    )
                if (
                    task.baseline.confirmed
                    or task.calls
                    or task.artifacts
                    or task.analysis_runs
                    or task.checkpoints
                    or task.checkpoint_sha256
                    or task.outputs
                    or task.result is not None
                    or any(row["task_id"] == task.id for row in artifacts)
                    or root.exists()
                ):
                    self.task_directory(record.id, task.id, task.output_root)
                roots[task.id] = root
            for row in artifacts:
                verify_evidence(
                    roots[str(row["task_id"])],
                    OutputArtifact.model_validate_json(row["descriptor_json"]),
                )
            for task in tasks.values():
                descriptors = [
                    *(
                        artifact
                        for output in task.outputs.values()
                        for artifact in output.artifacts
                    ),
                    *(
                        artifact
                        for run in task.analysis_runs
                        for artifact in [*run.artifacts, *run.output.artifacts]
                    ),
                    *([*task.result.artifacts] if task.result is not None else []),
                ]
                for descriptor in descriptors:
                    verify_evidence(roots[task.id], descriptor)
            events = [
                dict(row)
                for row in connection.execute(
                    """SELECT task_events.* FROM task_events
                       JOIN tasks ON tasks.id=task_events.task_id
                       WHERE tasks.conversation_id=? ORDER BY task_events.sequence""",
                    (conversation_id,),
                )
            ]
            payload = {
                "schema_version": 1,
                "conversation": record.model_dump(mode="json"),
                "artifact_index": artifacts,
                "task_events": events,
            }
            return (canonical(payload) + "\n").encode("utf-8")

    @staticmethod
    def _upsert(connection: sqlite3.Connection, table: str, row: Row) -> None:
        columns = list(row)
        keys = (
            ("conversation_id", "position")
            if table == "messages"
            else ("task_id", "slot", "position")
            if table == "artifacts"
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
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})",
            tuple(row.values()),
        )

    @staticmethod
    def _evidence_identity(path: Path) -> tuple[int, int, int, int, int, int]:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"Evidence is missing: {path.name}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Evidence changed identity: {path.name}")
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @classmethod
    def _capture_evidence_claim(
        cls,
        directory: Path,
        descriptor: OutputArtifact,
    ) -> _EvidenceClaim:
        path = directory / descriptor.path
        before = cls._evidence_identity(path)
        verified = verify_evidence(directory, descriptor)
        after = cls._evidence_identity(path)
        if verified != path or before != after:
            raise ValueError(f"Evidence changed while being verified: {descriptor.path}")
        return _EvidenceClaim(path=path, identity=after)

    @classmethod
    def _recheck_evidence_claims(cls, claims: list[_EvidenceClaim]) -> None:
        for claim in claims:
            if cls._evidence_identity(claim.path) != claim.identity:
                raise ValueError(
                    f"Evidence changed between verification and publication: "
                    f"{claim.path.name}"
                )

    def _prepare_evidence(
        self,
        record: Conversation,
        rows: dict[str, list[Row]],
        previous_artifacts: list[Row],
        previous_calls: list[Row],
    ) -> list[_EvidenceClaim]:
        previous_by_key = {
            (row["task_id"], row["slot"], row["position"]): row
            for row in previous_artifacts
        }
        changed = []
        for row in rows["artifacts"]:
            key = (row["task_id"], row["slot"], row["position"])
            previous = previous_by_key.get(key)
            if previous is not None:
                if previous != row:
                    raise ValueError("Persisted artifact rows are immutable")
                continue
            changed.append(row)
        tasks = {task.id: task for task in self._tasks(record)}
        previous_call_status = {
            str(row["id"]): str(row["status"]) for row in previous_calls
        }
        for task in tasks.values():
            runs = {run.id: run for run in task.analysis_runs}
            for call in task.calls:
                if (
                    call.status != "completed"
                    or previous_call_status.get(call.id) == "completed"
                ):
                    continue
                run = runs.get(call.id)
                if run is None:
                    raise ValueError("Completed call lacks its invocation receipt")
                directory = self.task_directory(
                    record.id, task.id, task.output_root
                )
                if load_agent_run(directory, call.id, run.spec) != run:
                    raise ValueError(
                        "Completed call lacks its verified disk receipt"
                    )
        claims: list[_EvidenceClaim] = []
        seen: set[tuple[Path, str, int]] = set()
        for row in changed:
            task = tasks[str(row["task_id"])]
            directory = self.task_directory(
                record.id, task.id, task.output_root
            )
            descriptor = OutputArtifact.model_validate_json(
                str(row["descriptor_json"])
            )
            artifact_key = (
                directory / descriptor.path,
                descriptor.sha256,
                descriptor.size,
            )
            if artifact_key in seen:
                continue
            seen.add(artifact_key)
            claims.append(self._capture_evidence_claim(directory, descriptor))
        return claims

    def _check_artifacts(
        self,
        connection: sqlite3.Connection,
        record: Conversation,
        rows: dict[str, list[Row]],
        claims: list[_EvidenceClaim],
    ) -> None:
        self._recheck_evidence_claims(claims)
        accepted_claims = getattr(self._local, "evidence_claims", None)
        if accepted_claims is not None:
            known = {(claim.path, claim.identity) for claim in accepted_claims}
            accepted_claims.extend(
                claim
                for claim in claims
                if (claim.path, claim.identity) not in known
            )
        tasks = {
            task.id: task
            for task in [
                *record.archived_tasks,
                *([record.task] if record.task else []),
            ]
        }
        for task in tasks.values():
            if not JOB_ID.fullmatch(task.id):
                raise ValueError("Invalid task ID")
            directory = (
                Path(task.output_root) / f"flight-agent-{task.id}"
                if task.output_root
                else self.default_task_root(record.id) / task.id
            )
            safe_directory(directory)
            if str(directory) != task.result_directory:
                raise ValueError(
                    "Task result directory does not match its persisted identity"
                )
            if task.baseline.confirmed:
                self.task_directory(record.id, task.id, task.output_root)
            previous_task = connection.execute(
                "SELECT conversation_id, state_json FROM tasks WHERE id=?", (task.id,)
            ).fetchone()
            if previous_task is None:
                continue
            if previous_task["conversation_id"] != record.id:
                raise ValueError("Stored ledger identity belongs to another owner")
            previous_state = json.loads(previous_task["state_json"])
            if (
                previous_state["result_directory"] != task.result_directory
                or previous_state["output_root"] != task.output_root
            ):
                raise ValueError("Task result directory identity is immutable")
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
                node = next(
                    (node for node in task.nodes if node.id == call.node_id), None
                )
                if (
                    node is None
                    or node.status != "completed"
                    or run.id != call.id
                    or run.spec.inputs != call.inputs
                    or run.spec.capability != call.tool
                    or run.spec.capability_version != call.tool_version
                    or call.baseline_version != task.baseline.version
                    or call.started_at != run.started_at
                    or call.finished_at != run.finished_at
                    or call.output != run.output.model_dump(mode="json")
                    or task.outputs.get(node.id) != run.output
                    or call.id in task.pending_specs
                    or previous is not None
                    and previous["spec_json"]
                    != canonical(run.spec.model_dump(mode="json"))
                ):
                    raise ValueError(
                        "Terminal call, node, output, and receipt disagree"
                    )
                if previous is not None and previous["status"] == "completed":
                    continue

    def save(self, record: Conversation) -> None:
        self.directory(record.id)
        validated = Conversation.model_validate(record.model_dump(mode="json"))
        self._check_size(validated)
        validated.updated_at = _now()
        with self._read() as connection:
            expected_artifacts = [
                dict(row)
                for row in connection.execute(
                    """SELECT artifacts.* FROM artifacts JOIN tasks ON tasks.id=artifacts.task_id
                       WHERE tasks.conversation_id=?
                       ORDER BY artifacts.task_id, artifacts.slot, artifacts.position""",
                    (record.id,),
                )
            ]
            expected_calls = [
                dict(row)
                for row in connection.execute(
                    """SELECT calls.* FROM calls JOIN tasks ON tasks.id=calls.task_id
                       WHERE tasks.conversation_id=?
                       ORDER BY calls.task_id, calls.position""",
                    (record.id,),
                )
            ]
        rows = encode(validated, expected_artifacts)
        claims = self._prepare_evidence(
            validated,
            rows,
            expected_artifacts,
            expected_calls,
        )
        with self.transaction():
            connection = self._write_connection()
            previous_artifacts = [
                dict(row)
                for row in connection.execute(
                    """SELECT artifacts.* FROM artifacts JOIN tasks ON tasks.id=artifacts.task_id
                       WHERE tasks.conversation_id=?
                       ORDER BY artifacts.task_id, artifacts.slot, artifacts.position""",
                    (record.id,),
                )
            ]
            if previous_artifacts != expected_artifacts:
                raise ValueError("Stored artifact ledger changed during publication")
            old_messages = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY position",
                    (record.id,),
                )
            ]
            if rows["messages"][: len(old_messages)] != old_messages:
                raise ValueError("Recorded messages are append-only")
            old_tasks = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM tasks WHERE conversation_id=?", (record.id,)
                )
            }
            if not set(old_tasks) <= {row["id"] for row in rows["tasks"]}:
                raise ValueError("Archived tasks cannot be dropped")
            for row in rows["tasks"]:
                previous = old_tasks.get(str(row["id"]))
                if previous is None:
                    continue
                row["created_at"] = previous["created_at"]
                result = json.loads(previous["result_json"])
                terminal = (
                    isinstance(result, dict)
                    and result.get("execution_status") in ("completed", "partial")
                )
                if not terminal:
                    continue
                immutable_fields = set(row) - {"archive_position", "updated_at"}
                if any(previous[field] != row[field] for field in immutable_fields):
                    raise ValueError("Terminal task payload is immutable")
                old_position = previous["archive_position"]
                new_position = row["archive_position"]
                if old_position is not None and new_position != old_position:
                    raise ValueError("Archived task position is immutable")
                row["updated_at"] = previous["updated_at"]
            old_calls = [
                dict(row)
                for row in connection.execute(
                    """SELECT calls.* FROM calls JOIN tasks ON tasks.id=calls.task_id
                       WHERE tasks.conversation_id=?
                       ORDER BY calls.task_id, calls.position""",
                    (record.id,),
                )
            ]
            if not {row["id"] for row in old_calls} <= {
                row["id"] for row in rows["calls"]
            }:
                raise ValueError("Recorded calls cannot be dropped")
            new_calls = {row["id"]: row for row in rows["calls"]}
            immutable_call_fields = (
                "task_id",
                "position",
                "node_id",
                "capability",
                "capability_version",
                "baseline_version",
                "inputs_json",
                "spec_json",
                "started_at",
            )
            for old_call in old_calls:
                new_call = new_calls[old_call["id"]]
                if (
                    new_call["status"] == "failed"
                    and new_call["spec_json"] == "null"
                    and old_call["spec_json"] != "null"
                ):
                    new_call["spec_json"] = old_call["spec_json"]
                if any(
                    old_call[field] != new_call[field]
                    for field in immutable_call_fields
                ):
                    raise ValueError("Recorded call identity fields are immutable")
                if (
                    old_call["status"] in ("completed", "failed")
                    and old_call != new_call
                ):
                    raise ValueError("Terminal call records are immutable")
            old_artifacts = {
                (row["task_id"], row["slot"], row["position"]): row
                for row in previous_artifacts
            }
            new_artifacts = {
                (row["task_id"], row["slot"], row["position"]): row
                for row in rows["artifacts"]
            }
            if not set(old_artifacts) <= set(new_artifacts):
                raise ValueError("Persisted artifact evidence cannot be dropped")
            if any(
                old_artifacts[key] != new_artifacts[key]
                for key in old_artifacts
            ):
                raise ValueError("Persisted artifact rows are immutable")
            self._check_artifacts(connection, validated, rows, claims)
            for table in ("conversations", "tasks", "messages", "calls", "artifacts"):
                for row in rows[table]:
                    keys = (
                        ("conversation_id", "position")
                        if table == "messages"
                        else ("task_id", "slot", "position")
                        if table == "artifacts"
                        else ("id",)
                    )
                    where = " AND ".join(f"{key}=?" for key in keys)
                    previous = connection.execute(
                        f"SELECT * FROM {table} WHERE {where}",
                        tuple(row[key] for key in keys),
                    ).fetchone()
                    if table in ("tasks", "calls") and previous is not None:
                        owner = "conversation_id" if table == "tasks" else "task_id"
                        if previous[owner] != row[owner]:
                            raise ValueError(
                                "Stored ledger identity belongs to another owner"
                            )
                    if table == "tasks" and previous is not None:
                        without_stamp = {
                            key: value
                            for key, value in row.items()
                            if key != "updated_at"
                        }
                        old_without_stamp = {
                            key: previous[key]
                            for key in previous.keys()
                            if key != "updated_at"
                        }
                        if without_stamp == old_without_stamp:
                            row["updated_at"] = previous["updated_at"]
                    if previous is not None and dict(previous) == row:
                        continue
                    self._upsert(connection, table, row)
            for row in rows["tasks"]:
                task_id = str(row["id"])
                old_task = old_tasks.get(task_id)
                before = self._state_digest(
                    old_task,
                    [call for call in old_calls if call["task_id"] == task_id],
                )
                after = self._state_digest(
                    row,
                    [
                        call
                        for call in rows["calls"]
                        if call["task_id"] == task_id
                    ],
                )
                if before != after:
                    connection.execute(
                        """INSERT INTO task_events(task_id, kind, created_at, payload_json)
                           VALUES (?, 'state_transition', ?, ?)""",
                        (
                            task_id,
                            validated.updated_at,
                            canonical(
                                {
                                    "from": before,
                                    "to": after,
                                    "stage": row["stage"],
                                    "status": row["status"],
                                }
                            ),
                        ),
                    )
        record.updated_at = validated.updated_at

    @staticmethod
    def _state_digest(task: Row | None, calls: list[Row]) -> str | None:
        if task is None:
            return None
        state = {
            "task": {
                key: value for key, value in task.items() if key != "updated_at"
            },
            "calls": sorted(calls, key=lambda row: int(row["position"] or 0)),
        }
        return hashlib.sha256(canonical(state).encode("utf-8")).hexdigest()
