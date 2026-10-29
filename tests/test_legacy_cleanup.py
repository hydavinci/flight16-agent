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
            "v1",
        ),
        (
            {"task": {"schema_version": "2.0"}, "archived_tasks": [{}]},
            "unknown",
        ),
        (
            {"schema_version": "2.0", "task": {"scenario": "d1"}},
            "v2",
        ),
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
            None,
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


@pytest.mark.parametrize(
    "data,message",
    [
        ({"task": {"scenario": "d1"}}, "Legacy conversation is unsupported; run cleanup-legacy"),
        ({"task": None}, "Conversation schema is unknown"),
    ],
)
def test_store_classifies_raw_records_without_deleting_them(tmp_path, data, message):
    store = ConversationStore(tmp_path)
    record = write_record(tmp_path / ("a" * 32), data)
    before = record.read_bytes()

    with pytest.raises(ValueError, match=message):
        store.get(record.parent.name)
    with pytest.raises(ValueError, match=message):
        ConversationStore(tmp_path)

    assert record.read_bytes() == before


def test_store_rejects_v2_marker_with_legacy_task_instead_of_cleaning_it(tmp_path):
    store = ConversationStore(tmp_path)
    record = write_record(
        tmp_path / ("a" * 32),
        {"schema_version": "2.0", "task": {"scenario": "d1"}},
    )
    before = record.read_bytes()

    with pytest.raises(ValueError):
        store.get(record.parent.name)

    assert record.read_bytes() == before
