import json

import pytest

from flight_agent.verification.workbench_store import WorkbenchRunStore
from flight_agent.workbench_contracts import WorkbenchRunRequest, workbench_request_digest


def workbench_request(run_id: str) -> WorkbenchRunRequest:
    return WorkbenchRunRequest(
        id=run_id,
        filename="calculation.m",
        source="result = struct('value', params.value * 2);",
        kind="script",
        parameters={"value": 3},
        trusted=True,
        timeout_seconds=120,
    )


def test_workbench_store_persists_request_status_and_recent_order(tmp_path):
    store = WorkbenchRunStore(tmp_path / "runs")
    first_request = workbench_request("a" * 32)
    first = store.create(first_request)
    second = store.create(workbench_request("b" * 32))

    assert store.request(first.id) == first_request
    assert store.get(first.id).request_sha256 == workbench_request_digest(first_request)
    assert [item.id for item in store.recent()] == [second.id, first.id]
    assert [item.id for item in store.recent(limit=1)] == [second.id]
    assert store.recent(limit=0) == []

    request_data = json.loads((store.directory(first.id) / "request.json").read_text("utf-8"))
    status_data = json.loads((store.directory(first.id) / "status.json").read_text("utf-8"))
    assert request_data["source"] == first_request.source
    assert "source" not in status_data


def test_workbench_store_marks_incomplete_runs_interrupted_after_restart(tmp_path):
    root = tmp_path / "runs"
    store = WorkbenchRunStore(root)
    queued = store.create(workbench_request("a" * 32))
    running = store.create(workbench_request("b" * 32))
    store.update(running.id, status="running", stage="executing")

    recovered = WorkbenchRunStore(root)
    for run_id in (queued.id, running.id):
        record = recovered.get(run_id)
        assert record.status == "interrupted"
        assert record.stage == "interrupted"
        assert "restart" in record.error.lower()


def test_workbench_store_hides_unknown_and_unsafe_paths(tmp_path):
    store = WorkbenchRunStore(tmp_path / "runs")
    for run_id in ("../secret", "a" * 32):
        with pytest.raises(KeyError):
            store.get(run_id)


def test_workbench_store_rejects_symlinked_run_status_and_request_paths(tmp_path):
    root = tmp_path / "runs"
    store = WorkbenchRunStore(root)

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ("a" * 32)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(KeyError):
        store.get("a" * 32)

    status_record = store.create(workbench_request("b" * 32))
    status_path = store.directory(status_record.id) / "status.json"
    outside_status = tmp_path / "outside-status.json"
    outside_status.write_text(status_path.read_text("utf-8"), encoding="utf-8")
    status_path.unlink()
    status_path.symlink_to(outside_status)
    with pytest.raises(KeyError):
        store.get(status_record.id)

    request_record = store.create(workbench_request("c" * 32))
    request_path = store.directory(request_record.id) / "request.json"
    outside_request = tmp_path / "outside-request.json"
    outside_request.write_text(request_path.read_text("utf-8"), encoding="utf-8")
    request_path.unlink()
    request_path.symlink_to(outside_request)
    with pytest.raises(ValueError, match="Unsafe run request"):
        store.request(request_record.id)


def test_workbench_store_update_changes_timestamp(tmp_path, monkeypatch):
    from flight_agent.verification import workbench_store

    monkeypatch.setattr(workbench_store, "now", lambda: "2026-11-02T16:00:00+00:00")
    store = WorkbenchRunStore(tmp_path / "runs")
    record = store.create(workbench_request("a" * 32))
    monkeypatch.setattr(workbench_store, "now", lambda: "2026-11-02T16:00:01+00:00")

    updated = store.update(record.id, status="running", stage="executing")

    assert updated.created_at == record.created_at
    assert updated.updated_at == "2026-11-02T16:00:01+00:00"


def test_workbench_store_create_existing_id_raises(tmp_path):
    store = WorkbenchRunStore(tmp_path / "runs")
    request = workbench_request("a" * 32)
    store.create(request)

    with pytest.raises(FileExistsError):
        store.create(request)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "b" * 32},
        {"source": "result = struct('value', params.value * 3);"},
    ],
)
def test_workbench_store_rejects_request_that_does_not_match_status(tmp_path, changes):
    store = WorkbenchRunStore(tmp_path / "runs")
    record = store.create(workbench_request("a" * 32))
    path = store.directory(record.id) / "request.json"
    data = json.loads(path.read_text("utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        store.request(record.id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "b" * 32),
        ("request_sha256", "c" * 64),
        ("filename", "replacement.m"),
        ("kind", "function"),
    ],
)
def test_workbench_store_rejects_updates_to_immutable_request_fields(
    tmp_path, field, value
):
    store = WorkbenchRunStore(tmp_path / "runs")
    record = store.create(workbench_request("a" * 32))

    with pytest.raises(ValueError, match="immutable"):
        store.update(record.id, **{field: value})

    assert store.get(record.id) == record
