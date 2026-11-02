import sqlite3

from ..runtime.agent_contracts import AgentBaseline
from ..runtime.project_snapshots import ProjectSnapshot
from .contracts import Conversation

SNAPSHOT_STATEMENTS = (
    """
    CREATE TABLE project_snapshot_references (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        resource_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL
            CHECK(length(snapshot_id) = 64 AND snapshot_id NOT GLOB '*[^0-9a-f]*'),
        source_fingerprint TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        PRIMARY KEY(task_id, resource_id)
    )
    """,
    """
    CREATE INDEX project_snapshot_references_by_snapshot
    ON project_snapshot_references(snapshot_id)
    """,
)


def _snapshot_rows(
    baseline: AgentBaseline,
) -> dict[str, tuple[str, str, str]]:
    if baseline.project_snapshots and not baseline.confirmed:
        raise ValueError("Unconfirmed tasks cannot retain project snapshots")
    expected: dict[str, tuple[str, str, str]] = {}
    for resource_id, raw_binding in baseline.project_snapshots.items():
        binding = ProjectSnapshot.model_validate(raw_binding.model_dump(mode="json"))
        if not binding.manifest.files:
            raise ValueError("Project snapshot manifest cannot be empty")
        expected[resource_id] = (
            binding.snapshot_id,
            binding.source_fingerprint,
            binding.manifest.canonical_bytes().decode("utf-8"),
        )
    return expected


def replace_snapshot_references(
    connection: sqlite3.Connection,
    record: Conversation,
) -> None:
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    for task in tasks:
        expected = _snapshot_rows(task.baseline)
        rows = connection.execute(
            "SELECT resource_id, snapshot_id, source_fingerprint, manifest_json "
            "FROM project_snapshot_references WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        existing = {
            str(row[0]): (str(row[1]), str(row[2]), str(row[3]))
            for row in rows
        }
        if existing and existing != expected:
            raise ValueError("Confirmed snapshot references are immutable")
        for resource_id, values in expected.items():
            connection.execute(
                "INSERT INTO project_snapshot_references "
                "(task_id, resource_id, snapshot_id, source_fingerprint, manifest_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id, resource_id) DO NOTHING",
                (task.id, resource_id, *values),
            )


def retained_snapshot_ids(connection: sqlite3.Connection) -> set[str]:
    expected: dict[tuple[str, str], tuple[str, str, str]] = {}
    for task_id, raw in connection.execute("SELECT id, baseline_json FROM tasks"):
        baseline = AgentBaseline.model_validate_json(raw)
        for resource_id, values in _snapshot_rows(baseline).items():
            expected[(str(task_id), resource_id)] = values
    actual = {
        (str(row[0]), str(row[1])): (str(row[2]), str(row[3]), str(row[4]))
        for row in connection.execute(
            "SELECT task_id, resource_id, snapshot_id, source_fingerprint, manifest_json "
            "FROM project_snapshot_references"
        )
    }
    if expected != actual:
        raise ValueError(
            "Snapshot reference index differs from retained task baselines"
        )
    return {entry[0] for entry in actual.values()}
