import importlib
import json

import pytest

from flight_agent.verification.store import ConversationStore


def cleanup_module():
    try:
        return importlib.import_module("flight_agent.verification.legacy_cleanup")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Raw record cleanup is missing: {exc}")


def write_record(directory, data):
    directory.mkdir()
    path = directory / "conversation.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("apply", [False, True])
def test_cleanup_cli_reports_all_classes_and_only_applies_to_v1(tmp_path, capsys, apply):
    from flight_agent.cli import main

    root = tmp_path / "runs" / "verification"
    root.mkdir(parents=True)
    v1 = write_record(root / ("a" * 32), {"task": {"scenario": "d1"}})
    v2 = write_record(root / ("b" * 32), {"schema_version": "2.0", "task": None})
    unknown = write_record(root / ("c" * 32), {"task": None})
    before = {path: path.read_bytes() for path in (v1, v2, unknown)}
    args = ["cleanup-legacy", "--project-root", str(tmp_path)]
    if apply:
        args.append("--apply")

    assert main(args) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "v1": ["a" * 32], "v2": ["b" * 32], "unknown": ["c" * 32],
        "deleted": ["a" * 32] if apply else [],
    }
    assert v1.exists() is not apply
    if not apply:
        assert v1.read_bytes() == before[v1]
    assert v2.read_bytes() == before[v2]
    assert unknown.read_bytes() == before[unknown]


@pytest.mark.parametrize(
    "data,expected",
    [
        (None, "unknown"),
        ([], "unknown"),
        ({}, "unknown"),
        ({"task": None}, "unknown"),
        ({"scenario": "d1", "task": None}, "unknown"),
        ({"task": {"scenario": "d1"}}, "v1"),
        ({"task": {"scenario": "d2"}}, "v1"),
        ({"archived_tasks": [{"scenario": "general"}]}, "v1"),
        ({"task": {"scenario": []}}, "unknown"),
        ({"task": {"scenario": {"name": "d1"}}}, "unknown"),
        ({"task": {"schema_version": "3.0"}}, "unknown"),
        ({"archived_tasks": "not a list"}, "unknown"),
        ({"schema_version": "2.0", "task": None}, "v2"),
        ({"task": {"schema_version": "2.0"}}, "v2"),
        ({"archived_tasks": [{"schema_version": "2.0"}]}, "v2"),
        (
            {"task": {"schema_version": "2.0"}, "archived_tasks": [{"scenario": "d1"}]},
            "unknown",
        ),
        (
            {"task": {"schema_version": "2.0"}, "archived_tasks": [{}]},
            "unknown",
        ),
        (
            {"schema_version": "2.0", "task": {"scenario": "d1"}},
            "unknown",
        ),
        ({"task": {"scenario": "d1"}, "archived_tasks": [None]}, "unknown"),
        ({"task": {"scenario": "d1"}, "archived_tasks": ["broken"]}, "unknown"),
        ({"task": {"scenario": "d1"}, "archived_tasks": {}}, "unknown"),
        ({"task": "broken", "archived_tasks": [{"scenario": "d1"}]}, "unknown"),
        ({"schema_version": "3.0", "task": {"scenario": "d1"}}, "unknown"),
        ({"schema_version": "3.0", "task": {"schema_version": "2.0"}}, "unknown"),
        ({"schema_version": None, "task": {"scenario": "d1"}}, "unknown"),
        ({"task": {"scenario": "d1", "schema_version": "3.0"}}, "unknown"),
        ({"task": {"scenario": "d1", "schema_version": "2.0"}}, "unknown"),
        ({"schema_version": "2.0", "archived_tasks": "broken"}, "unknown"),
        ({"schema_version": "1.0", "task": {"scenario": "d1"}}, "v1"),
    ],
)
def test_raw_record_classification_requires_positive_evidence(data, expected):
    assert cleanup_module().classify_record(data) == expected


def test_normalize_existing_v2_record_removes_only_legacy_envelope_fields():
    original = {
        "scenario": "auto",
        "goal": "",
        "active_workflow": None,
        "workflow_agenda": [],
        "task": {"schema_version": "2.0"},
        "archived_tasks": [{"schema_version": "2.0"}],
        "messages": [],
        "error": None,
        "project_root": "/project",
        "surprise": True,
    }
    before = json.loads(json.dumps(original))

    normalized = cleanup_module().normalize_v2_record(original)

    assert normalized == {
        "schema_version": "2.0",
        "task": {"schema_version": "2.0"},
        "archived_tasks": [{"schema_version": "2.0"}],
        "messages": [],
        "error": None,
        "project_root": "/project",
        "surprise": True,
    }
    assert original == before
    assert cleanup_module().normalize_v2_record(normalized) == normalized


@pytest.mark.parametrize("apply", [False, True])
def test_cleanup_deletes_only_positive_v1_records(tmp_path, apply):
    v1 = write_record(tmp_path / ("a" * 32), {"task": {"scenario": "d1"}})
    v2 = write_record(
        tmp_path / ("b" * 32),
        {"schema_version": "2.0", "task": {"schema_version": "2.0"}},
    )
    unknown = write_record(tmp_path / ("c" * 32), {"task": None})
    before = {path: path.read_bytes() for path in (v1, v2, unknown)}

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=apply)

    assert v1.parent.exists() is not apply
    assert v2.read_bytes() == before[v2]
    assert unknown.read_bytes() == before[unknown]
    assert report.v1 == [v1.parent.name]
    assert report.v2 == [v2.parent.name]
    assert report.deleted == ([v1.parent.name] if apply else [])
    assert report.unknown == [unknown.parent.name]
    if not apply:
        assert v1.read_bytes() == before[v1]


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("source_location", ["top_level", "current_task", "archived_task"])
@pytest.mark.parametrize("source_path", ["candidate", "descendant", "symlink_alias"])
def test_cleanup_preserves_recorded_source_project_inside_candidate(
    tmp_path, apply, source_location, source_path
):
    root = tmp_path / "state"
    root.mkdir()
    candidate = root / ("a" * 32)
    record = write_record(candidate, {})
    (tmp_path / "external-source").mkdir()
    source = candidate if source_path == "candidate" else candidate / "source"
    source.mkdir(exist_ok=True)
    sentinel = source / "model.slx"
    sentinel.write_bytes(b"source project must survive")
    recorded_source = source
    if source_path == "symlink_alias":
        recorded_source = tmp_path / "source-alias"
        recorded_source.symlink_to(source, target_is_directory=True)
    data = {
        "project_root": str(tmp_path / "external-source"),
        "task": {"scenario": "d1", "project": {"root": str(tmp_path / "external-source")}},
    }
    if source_location == "top_level":
        data["project_root"] = str(recorded_source)
    elif source_location == "current_task":
        data["task"]["project"]["root"] = str(recorded_source)
    else:
        data["archived_tasks"] = [
            {"scenario": "d2", "project": {"root": str(recorded_source)}},
        ]
    record.write_text(json.dumps(data), encoding="utf-8")
    before = record.read_bytes()

    report = cleanup_module().cleanup_legacy_records(root, apply=apply)

    assert sentinel.is_file()
    assert sentinel.read_bytes() == b"source project must survive"
    assert record.read_bytes() == before
    assert report.v1 == [candidate.name]
    assert report.deleted == []
    assert report.unknown == []


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize(
    "reference", [
        "project_root", "output_root", "result_directory", "task_output", "resource",
        "worker_resource", "diagnostic_resource",
    ]
)
def test_cleanup_preserves_paths_owned_by_another_v2_record(
    tmp_path, apply, archived, reference
):
    candidate = write_record(tmp_path / ("a" * 32), {"task": {"scenario": "d1"}})
    protected = candidate.parent / "retained"
    protected.mkdir()
    sentinel = protected / "keep.json"
    sentinel.write_bytes(b"v2 source, evidence or delivery")
    task = {"schema_version": "2.0"}
    data = {"schema_version": "2.0"}
    if reference in ("project_root", "output_root"):
        data[reference] = str(protected)
    elif reference in ("result_directory", "task_output"):
        task["output_root" if reference == "task_output" else reference] = str(protected)
    else:
        task["baseline"] = {"resources": [{
            "id": "source",
            "kind": {
                "resource": "matlab_project", "worker_resource": "simulink_project",
                "diagnostic_resource": "diagnostic_data",
            }[reference],
            "location": "worker" if reference == "worker_resource" else "agent",
            "locator": str(protected), "fingerprint": "a" * 64,
        }]}
    if archived:
        data["archived_tasks"] = [task]
    else:
        data["task"] = task
    retained = write_record(tmp_path / ("b" * 32), data)
    before = {path: path.read_bytes() for path in (candidate, retained, sentinel)}

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=apply)

    assert report.deleted == []
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("evidence", [
    "missing", "nul", "symlink_loop", "relative", "malformed_resource", "unknown_location",
    "unreadable_record", "missing_project_root", "null_project_root", "malformed_task",
    "missing_artifact_owner",
])
def test_cleanup_fails_closed_on_unresolvable_retained_record_paths(tmp_path, evidence):
    candidate = write_record(tmp_path / ("a" * 32), {"task": {"scenario": "d1"}})
    source = tmp_path / "missing"
    if evidence == "nul":
        source = tmp_path / "bad\0path"
    elif evidence == "symlink_loop":
        source.symlink_to(source)
    data = {"schema_version": "2.0", "project_root": str(source)}
    if evidence == "relative":
        data["project_root"] = "relative-source"
    elif evidence in ("malformed_resource", "unknown_location"):
        data = {"schema_version": "2.0", "task": {
            "schema_version": "2.0",
            "baseline": {"resources": [None if evidence == "malformed_resource" else {
                "id": "source", "kind": "project", "location": "future_location",
                "locator": str(tmp_path), "fingerprint": "a" * 64,
            }]},
        }}
    elif evidence in ("missing_project_root", "null_project_root"):
        data = {"schema_version": "2.0", "task": {
            "schema_version": "2.0",
            "project": {} if evidence == "missing_project_root" else {"root": None},
        }}
    elif evidence == "malformed_task":
        data = {"task": {"unexpected_paths": [str(tmp_path)]}}
    elif evidence == "missing_artifact_owner":
        data = {"schema_version": "2.0", "task": {
            "schema_version": "2.0",
            "artifacts": [{"path": "keep.json", "sha256": "a" * 64, "size": 1}],
        }}
    retained = write_record(tmp_path / ("b" * 32), data)
    if evidence == "unreadable_record":
        retained.write_bytes(b"{")
    before = candidate.read_bytes(), retained.read_bytes()

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=True)

    assert report.deleted == []
    assert (candidate.read_bytes(), retained.read_bytes()) == before


def test_cleanup_protects_delivery_referenced_by_a_retained_v1_record(tmp_path):
    candidate = write_record(tmp_path / ("a" * 32), {"task": {"scenario": "d1"}})
    delivery = candidate.parent / "delivery"
    delivery.mkdir()
    sentinel = delivery / "keep.json"
    sentinel.write_bytes(b"retained delivery")
    retained = write_record(tmp_path / ("b" * 32), {
        "project_root": str(tmp_path / ("b" * 32)),
        "task": {"scenario": "d2", "result_directory": str(delivery)},
    })

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=True)

    assert report.deleted == []
    assert retained.exists()
    assert sentinel.read_bytes() == b"retained delivery"


def test_cleanup_preserves_mixed_record_instead_of_deleting_its_v2_evidence(tmp_path):
    record = write_record(tmp_path / ("a" * 32), {
        "task": {"scenario": "d1"},
        "archived_tasks": [{"schema_version": "2.0"}],
    })
    before = record.read_bytes()

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=True)

    assert report.unknown == [record.parent.name]
    assert report.deleted == []
    assert record.read_bytes() == before


def test_cleanup_with_external_source_project_still_deletes_only_conversation(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    source = tmp_path / "external-source"
    source.mkdir()
    sentinel = source / "model.slx"
    sentinel.write_bytes(b"external source must survive")
    record = write_record(
        root / ("a" * 32),
        {
            "project_root": str(source),
            "task": {"scenario": "d1", "project": {"root": str(source)}},
            "archived_tasks": [{"scenario": "d2", "project": {"root": str(source)}}],
        },
    )

    report = cleanup_module().cleanup_legacy_records(root, apply=True)

    assert sentinel.read_bytes() == b"external source must survive"
    assert not record.parent.exists()
    assert report.v1 == [record.parent.name]
    assert report.deleted == [record.parent.name]


@pytest.mark.parametrize("source_path", ["invalid_path", "symlink_loop"])
def test_cleanup_refuses_deletion_when_recorded_source_cannot_be_resolved(tmp_path, source_path):
    root = tmp_path / "state"
    root.mkdir()
    source = tmp_path / "bad\0root"
    if source_path == "symlink_loop":
        source = tmp_path / "source-loop"
        source.symlink_to(source)
    record = write_record(
        root / ("a" * 32),
        {"project_root": str(source), "task": {"scenario": "d1"}},
    )
    sentinel = record.parent / "model.slx"
    sentinel.write_bytes(b"unverified source must survive")

    report = cleanup_module().cleanup_legacy_records(root, apply=True)

    assert sentinel.is_file()
    assert sentinel.read_bytes() == b"unverified source must survive"
    assert report.v1 == [record.parent.name]
    assert report.deleted == []


@pytest.mark.parametrize(
    "payload", [b"{", b"\xff", b" " * 8_000_001], ids=["invalid-json", "invalid-utf8", "oversized"]
)
def test_cleanup_preserves_unreadable_or_oversized_records(tmp_path, payload):
    record = write_record(tmp_path / ("a" * 32), {})
    record.write_bytes(payload)

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=True)

    assert record.read_bytes() == payload
    assert report.deleted == []
    assert report.unknown == [record.parent.name]


def test_cleanup_ignores_invalid_ids_and_nonconversation_files(tmp_path):
    invalid = write_record(tmp_path / "not-a-conversation-id", {"task": {"scenario": "d1"}})
    worker = tmp_path / ("a" * 32)
    worker.mkdir()
    (worker / "status.json").write_text('{"status":"completed"}')
    plain_file = tmp_path / ("b" * 32)
    plain_file.write_text("not a directory")

    report = cleanup_module().cleanup_legacy_records(tmp_path, apply=True)

    assert invalid.exists()
    assert (worker / "status.json").exists()
    assert plain_file.exists()
    assert report.deleted == []


@pytest.mark.parametrize("link_kind", ["directory", "record"])
def test_cleanup_rejects_symlink_candidates_without_touching_target(tmp_path, link_kind):
    root = tmp_path / "state"
    root.mkdir()
    target = write_record(tmp_path / "outside", {"task": {"scenario": "d1"}})
    candidate = root / ("a" * 32)
    if link_kind == "directory":
        candidate.symlink_to(target.parent, target_is_directory=True)
    else:
        candidate.mkdir()
        (candidate / "conversation.json").symlink_to(target)

    report = cleanup_module().cleanup_legacy_records(root, apply=True)

    assert target.exists()
    assert candidate.exists()
    assert report.deleted == []
    assert report.unknown == [candidate.name]


def test_cleanup_rejects_symlink_root(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    record = write_record(target / ("a" * 32), {"task": {"scenario": "d1"}})
    root = tmp_path / "alias"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        cleanup_module().cleanup_legacy_records(root, apply=True)

    assert record.exists()


def test_cleanup_does_not_follow_nested_symlinks_or_external_output_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    external = tmp_path / "delivery"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("keep delivery")
    record = write_record(
        root / ("a" * 32),
        {"task": {"scenario": "d1", "result_directory": str(external)}},
    )
    (record.parent / "delivery-link").symlink_to(external, target_is_directory=True)

    report = cleanup_module().cleanup_legacy_records(root, apply=True)

    assert report.deleted == [record.parent.name]
    assert not record.parent.exists()
    assert sentinel.read_text() == "keep delivery"


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


def test_cleanup_preserves_sqlite_owned_external_result_and_deletes_unrelated_v1(tmp_path):
    from tests.agent_helpers import analysis_plan_data
    from tests.test_agent_runtime import confirm, propose, setup

    executor, store, record, _ = setup(tmp_path)
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    unrelated = write_record(store.root / ("b" * 32), {"task": {"scenario": "d2"}})
    record.output_root = str(legacy.parent / "output")
    data = analysis_plan_data()
    data["allowed_effects"].append("write_delivery")
    done = confirm(executor, propose(executor, record, data))
    assert done.status == "completed", done.error
    evidence = next(path for path in (legacy.parent / "output").rglob("*") if path.is_file())
    before = evidence.read_bytes()

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert evidence.read_bytes() == before
    assert legacy.exists()
    assert not unrelated.exists()
    assert report.deleted == ["b" * 32]
    assert store.get(done.id).task.analysis_runs == done.task.analysis_runs


@pytest.mark.parametrize("damage", ["corrupt", "future"])
def test_cleanup_fails_closed_when_sqlite_ownership_is_unreadable(tmp_path, damage):
    import sqlite3

    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    if damage == "future":
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("PRAGMA user_version=999")
    else:
        store.database_path.write_bytes(b"not a database")
    before = store.database_path.read_bytes()

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert record.exists()
    assert store.database_path.read_bytes() == before


@pytest.mark.parametrize("owner", ["conversation_source", "task_resource", "unconfirmed_output"])
@pytest.mark.parametrize("archived", [False, True])
def test_cleanup_protects_sqlite_source_and_unconfirmed_output_paths(tmp_path, owner, archived):
    from flight_agent.runtime.resources import ResourceReference
    from tests.test_agent_history import agent_task

    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    source = legacy.parent / "source"
    source.mkdir()
    sentinel = source / "keep.txt"
    sentinel.write_text("live source")
    if owner == "conversation_source":
        record.project_root = str(source)
    else:
        task_id = "b" * 32
        output = source if owner == "unconfirmed_output" else None
        directory = (
            output / f"flight-agent-{task_id}"
            if output
            else store.default_task_root(record.id) / task_id
        )
        task = agent_task(directory, output_root=str(output) if output else None)
        task.id = task_id
        if owner == "task_resource":
            task.baseline = task.baseline.model_copy(update={"resources": [
                ResourceReference(
                    id="source", kind="data", location="agent", locator=str(source),
                    fingerprint="a" * 64,
                )
            ]})
        if archived:
            record.archived_tasks = [task]
        else:
            record.task = task
    store.save(record)
    database_before = store.database_path.read_bytes()

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert sentinel.read_text() == "live source"
    assert store.database_path.read_bytes() == database_before


def test_cleanup_fails_closed_for_missing_sqlite_evidence_owner(tmp_path):
    from flight_agent.runtime.evidence import describe_evidence
    from flight_agent.runtime.observations import OutputArtifact
    from tests.test_agent_history import agent_task

    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    directory = store.task_directory(record.id, "b" * 32, create=True)
    record.task = agent_task(directory).model_copy(update={"id": "b" * 32})
    evidence = directory / "evidence.json"
    evidence.write_text("{}")
    record.task.artifacts.append(
        OutputArtifact(**describe_evidence(directory, evidence.name).model_dump())
    )
    store.save(record)
    evidence.unlink()
    directory.rmdir()

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert legacy.exists()


def test_cleanup_preserves_candidate_inside_sqlite_owned_source(tmp_path):
    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    evidence = legacy.parent / "model.slx"
    evidence.write_text("model")
    record.project_root = str(store.root)
    store.save(record)

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert evidence.read_text() == "model"


@pytest.mark.parametrize("owner", ["conversation_source", "task_resource"])
def test_cleanup_preserves_lexical_symlink_locator_inside_v1_candidate(tmp_path, owner):
    from flight_agent.runtime.resources import ResourceReference
    from tests.test_agent_history import agent_task

    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    target = tmp_path / "external"
    target.mkdir()
    sentinel = target / "model.slx"
    sentinel.write_bytes(b"source")
    locator = legacy.parent / "source-link"
    locator.symlink_to(target, target_is_directory=True)
    if owner == "conversation_source":
        record.project_root = str(locator)
    else:
        task = agent_task(store.default_task_root(record.id) / ("b" * 32))
        task.id = "b" * 32
        task.baseline = task.baseline.model_copy(update={"resources": [
            ResourceReference(
                id="source", kind="data", location="agent", locator=str(locator),
                fingerprint="a" * 64,
            )
        ]})
        record.task = task
    store.save(record)

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert legacy.exists()
    assert locator.is_symlink()
    assert sentinel.read_bytes() == b"source"


@pytest.mark.parametrize("apply", [False, True])
def test_cleanup_inventory_does_not_create_live_wal_shared_memory(tmp_path, apply):
    import os
    import subprocess
    import sys

    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    script = """
import os
import sqlite3
connection = sqlite3.connect(os.environ["DATABASE"])
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("UPDATE conversations SET error='wal-only'")
connection.commit()
os._exit(73)
"""
    environment = dict(os.environ, DATABASE=str(store.database_path))
    process = subprocess.run(
        [sys.executable, "-c", script], env=environment, timeout=15, capture_output=True
    )
    assert process.returncode == 73, process.stderr
    wal = store.root / "conversations.sqlite3-wal"
    shm = store.root / "conversations.sqlite3-shm"
    assert wal.is_file() and wal.stat().st_size > 32
    if shm.exists():
        shm.unlink()

    def inventory():
        return {
            str(path.relative_to(store.root)): path.read_bytes() if path.is_file() else None
            for path in store.root.rglob("*")
        }

    before = inventory()
    report = cleanup_module().cleanup_legacy_records(store.root, apply=apply)
    assert report.deleted == []
    assert report.unknown == []
    assert inventory() == before
    assert not shm.exists()
    assert record.id in {path.name for path in store.root.iterdir()}


def test_cleanup_preserves_json_source_locator_inside_v1_candidate(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    legacy = write_record(root / ("a" * 32), {"task": {"scenario": "d1"}})
    external = tmp_path / "source"
    external.mkdir()
    locator = legacy.parent / "source-link"
    locator.symlink_to(external, target_is_directory=True)
    write_record(root / ("b" * 32), {"schema_version": "2.0", "project_root": str(locator)})

    report = cleanup_module().cleanup_legacy_records(root, apply=True)

    assert report.deleted == []
    assert legacy.exists()
    assert locator.is_symlink()


@pytest.mark.parametrize("owner", ["conversation_source", "task_resource"])
def test_cleanup_preserves_candidate_traversed_by_external_symlink_chain(tmp_path, owner):
    from flight_agent.runtime.resources import ResourceReference
    from tests.test_agent_history import agent_task

    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    outside = tmp_path / "outside"
    outside.mkdir()
    endpoint = outside / "endpoint"
    endpoint.mkdir()
    sentinel = endpoint / "model.slx"
    sentinel.write_bytes(b"source")
    bridge = legacy.parent / "bridge"
    bridge.symlink_to(endpoint, target_is_directory=True)
    locator = outside / "locator"
    locator.symlink_to(
        f"../state/{legacy.parent.name}/{bridge.name}",
        target_is_directory=True,
    )
    assert locator.resolve() == endpoint
    if owner == "conversation_source":
        record.project_root = str(locator)
    else:
        task = agent_task(store.default_task_root(record.id) / ("b" * 32))
        task.id = "b" * 32
        task.baseline = task.baseline.model_copy(update={"resources": [
            ResourceReference(
                id="source", kind="data", location="agent", locator=str(locator),
                fingerprint="a" * 64,
            )
        ]})
        record.task = task
    store.save(record)

    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert legacy.exists()
    assert locator.resolve() == endpoint
    assert sentinel.read_bytes() == b"source"


@pytest.mark.parametrize("damage", ["cycle", "missing", "nonregular", "ambiguous"])
def test_cleanup_fails_closed_for_unresolvable_owned_symlink_traversal(
    tmp_path, monkeypatch, damage
):
    module = cleanup_module()
    store = ConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    outside = tmp_path / "outside"
    outside.mkdir()
    locator = outside / "locator"
    if damage == "cycle":
        other = outside / "other"
        locator.symlink_to(other)
        other.symlink_to(locator)
    elif damage == "missing":
        locator.symlink_to(outside / "missing")
    elif damage == "nonregular":
        file = outside / "file"
        file.write_text("not a directory")
        locator = file / "child"
    else:
        first = outside / "first"
        second = outside / "second"
        first.mkdir()
        second.mkdir()
        locator.symlink_to(first)
    record.project_root = str(locator)
    store.save(record)
    if damage == "ambiguous":
        readlink = module.os.readlink
        reads = 0

        def changing_target(path):
            nonlocal reads
            target = readlink(path)
            if str(path) == str(locator):
                reads += 1
                return target if reads == 1 else str(outside / "second")
            return target

        monkeypatch.setattr(module.os, "readlink", changing_target)

    report = module.cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert legacy.exists()


@pytest.mark.parametrize("change", ["database", "sidecar"])
def test_cleanup_refuses_deletion_when_sqlite_changes_during_inventory(
    tmp_path, monkeypatch, change
):
    module = cleanup_module()
    store = ConversationStore(tmp_path / "state", require_empty=True)
    legacy = write_record(store.root / ("a" * 32), {"task": {"scenario": "d1"}})
    inspect = module._sqlite_snapshot_paths

    def change_after_inspection(root, snapshot):
        result = inspect(root, snapshot)
        if change == "database":
            store.database_path.write_bytes(store.database_path.read_bytes() + b"change")
        else:
            (store.root / "conversations.sqlite3-journal").write_bytes(b"new sidecar")
        return result

    monkeypatch.setattr(module, "_sqlite_snapshot_paths", change_after_inspection)
    report = module.cleanup_legacy_records(store.root, apply=True)

    assert report.deleted == []
    assert report.unknown == ["conversations.sqlite3"]
    assert legacy.exists()


def test_cleanup_inventories_hot_journal_without_recovering_live_files(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    store = ConversationStore(tmp_path / "state", require_empty=True)
    store.create()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        connection.execute("CREATE TABLE pressure (id INTEGER PRIMARY KEY, payload BLOB)")
        connection.executemany("INSERT INTO pressure (payload) VALUES (?)", [(b"a" * 2000,)] * 300)
    script = """
import os
import sqlite3
connection = sqlite3.connect(os.environ["DATABASE"], isolation_level=None)
connection.execute("PRAGMA cache_size=8")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE pressure SET payload=?", (b"b" * 2000,))
os._exit(73)
"""
    process = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, DATABASE=str(store.database_path)),
        timeout=15,
        capture_output=True,
    )
    assert process.returncode == 73, process.stderr
    journal = store.root / "conversations.sqlite3-journal"
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")

    def inventory():
        return {
            str(path.relative_to(store.root)): path.read_bytes() if path.is_file() else None
            for path in store.root.rglob("*")
        }

    before = inventory()
    report = cleanup_module().cleanup_legacy_records(store.root, apply=True)
    assert report.unknown == []
    assert report.deleted == []
    assert inventory() == before
