import shutil
import sqlite3
from copy import deepcopy

import pytest

from flight_agent.runtime.agent_contracts import AgentBaseline
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.project_snapshots import (
    ProjectSnapshot,
    SnapshotFile,
    SnapshotManifest,
)
from flight_agent.verification.contracts import AgentExecutionNode, AgentTask
from flight_agent.verification.snapshot_references import retained_snapshot_ids
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import analysis_plan_data
from tests.snapshot_helpers import snapshot_case


def _task_with_binding(store, conversation_id, binding, task_id):
    data = analysis_plan_data()
    data["resources"] = [
        {
            "id": "project",
            "kind": "matlab_project",
            "location": "agent",
            "locator": "/source/original",
            "fingerprint": binding.source_fingerprint,
            "version": "1",
            "metadata": {},
        }
    ]
    data["nodes"][0]["resource_bindings"] = {"project": "project"}
    baseline = AgentBaseline.model_validate(
        {
            **data,
            "version": 1,
            "confirmed": True,
            "confirmed_at": "2026-10-31T00:00:00+00:00",
            "project_snapshots": {"project": binding.model_dump(mode="json")},
        }
    )
    directory = store.task_directory(conversation_id, task_id, create=True)
    return AgentTask(
        id=task_id,
        baseline=baseline,
        baseline_sha256=fingerprint(baseline.model_dump(mode="json")),
        nodes=[
            AgentExecutionNode(**node.model_dump(mode="json"))
            for node in baseline.nodes
        ],
        result_directory=str(directory),
    )


def _record_with_binding(store, binding):
    record = store.create()
    record.task = _task_with_binding(store, record.id, binding, "d" * 32)
    return record


def _different_binding(binding):
    files = [
        *binding.manifest.files,
        SnapshotFile(path="zz-retained-marker", sha256="e" * 64, size=1),
    ]
    manifest = SnapshotManifest(files=sorted(files, key=lambda item: item.path))
    return ProjectSnapshot(
        snapshot_id=manifest.snapshot_id,
        source_fingerprint="f" * 64,
        manifest=manifest,
    )


def test_snapshot_reference_commits_with_baseline_and_survives_restart(tmp_path):
    source, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    record = _record_with_binding(store, binding)
    with snapshots.guard(), store.transaction():
        store.save(record)
    shutil.rmtree(source)
    restarted = ConversationStore(store.root)
    with restarted._read() as connection:
        assert retained_snapshot_ids(connection) == {binding.snapshot_id}
    assert restarted.get(record.id).task.baseline.project_snapshots["project"] == binding


def test_failed_transaction_registers_neither_baseline_state_nor_reference(tmp_path):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    record = _record_with_binding(store, binding)
    with pytest.raises(RuntimeError, match="abort"):
        with snapshots.guard(), store.transaction():
            store.save(record)
            raise RuntimeError("abort confirmation")
    assert store.get(record.id).task is None
    with store._read() as connection:
        assert retained_snapshot_ids(connection) == set()
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 0


def test_saved_confirmed_binding_cannot_be_removed_or_changed(tmp_path):
    _, _, _, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    record = _record_with_binding(store, binding)
    store.save(record)

    removed = deepcopy(record)
    removed.task.baseline = removed.task.baseline.model_copy(
        update={"project_snapshots": {}}
    )
    removed.task.baseline_sha256 = fingerprint(
        removed.task.baseline.model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="immutable"):
        store.save(removed)

    changed = deepcopy(record)
    replacement = _different_binding(binding)
    changed.task.baseline = changed.task.baseline.model_copy(
        update={"project_snapshots": {"project": replacement}}
    )
    changed.task.baseline_sha256 = fingerprint(
        changed.task.baseline.model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="immutable"):
        store.save(changed)

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DELETE FROM project_snapshot_references")
    with pytest.raises(ValueError, match="immutable"):
        store.save(changed)

    saved = store.get(record.id)
    assert saved.task.baseline.project_snapshots["project"] == binding


def test_unconfirmed_or_empty_snapshot_binding_is_rejected(tmp_path):
    _, _, _, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    unconfirmed = _record_with_binding(store, binding)
    unconfirmed.task.baseline = unconfirmed.task.baseline.model_copy(
        update={"confirmed": False, "confirmed_at": None}
    )
    unconfirmed.task.baseline_sha256 = fingerprint(
        unconfirmed.task.baseline.model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="Unconfirmed"):
        store.save(unconfirmed)

    manifest = SnapshotManifest(files=[])
    empty_binding = ProjectSnapshot(
        snapshot_id=manifest.snapshot_id,
        source_fingerprint="a" * 64,
        manifest=manifest,
    )
    empty = _record_with_binding(store, empty_binding)
    with pytest.raises(ValueError, match="empty"):
        store.save(empty)


def test_retained_snapshot_ids_cover_current_and_archived_tasks_and_detect_drift(
    tmp_path,
):
    _, _, _, binding = snapshot_case(tmp_path)
    archived_binding = _different_binding(binding)
    store = ConversationStore(tmp_path / "state")
    record = _record_with_binding(store, binding)
    record.archived_tasks = [
        _task_with_binding(store, record.id, archived_binding, "e" * 32)
    ]
    store.save(record)
    with store._read() as connection:
        assert retained_snapshot_ids(connection) == {
            binding.snapshot_id,
            archived_binding.snapshot_id,
        }

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "DELETE FROM project_snapshot_references WHERE task_id=?",
            (record.archived_tasks[0].id,),
        )
    with store._read() as connection, pytest.raises(
        ValueError, match="differs from retained task baselines"
    ):
        retained_snapshot_ids(connection)


def test_snapshot_reference_foreign_key_rejects_unknown_task(tmp_path):
    store = ConversationStore(tmp_path / "state")
    with store._read() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO project_snapshot_references
               (task_id, resource_id, snapshot_id, source_fingerprint, manifest_json)
               VALUES (?, ?, ?, ?, ?)""",
            ("f" * 32, "project", "a" * 64, "b" * 64, '{"files":[]}'),
        )
