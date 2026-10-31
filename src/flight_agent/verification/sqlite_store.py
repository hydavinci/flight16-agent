import hashlib
import json
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

    def _check_artifacts(
        self,
        connection: sqlite3.Connection,
        record: Conversation,
        rows: dict[str, list[Row]],
    ) -> None:
        tasks = {
            task.id: task
            for task in [
                *record.archived_tasks,
                *([record.task] if record.task else []),
            ]
        }
        for task in tasks.values():
            directory = self.task_directory(record.id, task.id, task.output_root)
            if str(directory) != task.result_directory:
                raise ValueError(
                    "Task result directory does not match its persisted identity"
                )
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
        for row in rows["artifacts"]:
            task = tasks[str(row["task_id"])]
            if row["slot"] in ("receipt", "checkpoint"):
                previous = connection.execute(
                    "SELECT * FROM artifacts WHERE task_id=? AND slot=? AND call_id=?",
                    (row["task_id"], row["slot"], row["call_id"]),
                ).fetchone()
            else:
                previous = connection.execute(
                    "SELECT * FROM artifacts WHERE task_id=? AND slot=? AND position=?",
                    (row["task_id"], row["slot"], row["position"]),
                ).fetchone()
            if previous is not None and dict(previous) == row:
                continue
            if previous is not None and row["slot"] in ("receipt", "checkpoint"):
                stable = dict(previous)
                stable["position"] = row["position"]
                if stable == row:
                    continue
            directory = self.task_directory(record.id, task.id, task.output_root)
            verify_evidence(
                directory,
                OutputArtifact.model_validate_json(str(row["descriptor_json"])),
            )
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
                directory = self.task_directory(
                    record.id, task.id, task.output_root
                )
                if load_agent_run(directory, call.id, run.spec) != run:
                    raise ValueError(
                        "Completed call lacks its verified disk receipt"
                    )

    def save(self, record: Conversation) -> None:
        self.directory(record.id)
        validated = Conversation.model_validate(record.model_dump(mode="json"))
        if (
            len(canonical(validated.model_dump(mode="json")).encode("utf-8"))
            > 8_000_000
        ):
            raise ValueError(
                "Conversation exceeds 8 MB; existing state was not replaced"
            )
        validated.updated_at = _now()
        with self.transaction():
            connection = self._local.connection
            previous_artifacts = [
                dict(row)
                for row in connection.execute(
                    """SELECT artifacts.* FROM artifacts JOIN tasks ON tasks.id=artifacts.task_id
                       WHERE tasks.conversation_id=?""",
                    (record.id,),
                )
            ]
            rows = encode(validated, previous_artifacts)
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
            old_artifact_keys = {
                (row["task_id"], row["slot"], row["position"])
                for row in previous_artifacts
            }
            new_artifact_keys = {
                (row["task_id"], row["slot"], row["position"])
                for row in rows["artifacts"]
            }
            if not old_artifact_keys <= new_artifact_keys:
                raise ValueError("Persisted artifact evidence cannot be dropped")
            self._check_artifacts(connection, validated, rows)
            for row in rows["tasks"]:
                previous = old_tasks.get(str(row["id"]))
                if previous is not None:
                    row["created_at"] = previous["created_at"]
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
