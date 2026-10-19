import importlib
import json

import pytest

from flight_agent.runtime.contracts import AnalysisRun, AnalysisSpec, fingerprint
from tests.test_runtime_kernel import spec_data


def evidence():
    try:
        return importlib.import_module("flight_agent.runtime.evidence")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Shared evidence store is missing: {exc.name}")


def run(**changes):
    data = {
        "id": "1" * 32,
        "spec": AnalysisSpec.model_validate(spec_data()),
        "output": {"value": 1.0},
        "output_sha256": fingerprint({"value": 1.0}),
        "started_at": "2026-10-19T00:00:00Z",
        "finished_at": "2026-10-19T00:00:01Z",
    }
    data.update(changes)
    return AnalysisRun.model_validate(data)


def test_receipts_are_immutable_idempotent_and_bound_to_full_spec(tmp_path):
    store = evidence()
    original = run()
    path = store.persist_run(tmp_path, original)
    assert store.load_run(tmp_path, original.id, original.spec) == original
    assert store.persist_run(tmp_path, original) == path
    changed = run(output={"value": 2.0}, output_sha256=fingerprint({"value": 2.0}))
    with pytest.raises(ValueError, match="immutable|different"):
        store.persist_run(tmp_path, changed)
    changed_spec = original.spec.model_copy(update={"inputs": {"observed": "other"}})
    with pytest.raises(ValueError, match="spec|request"):
        store.load_run(tmp_path, original.id, changed_spec)
    assert store.load_run(tmp_path, "2" * 32, original.spec) is None


def test_evidence_cache_verifies_output_digest_and_every_artifact(tmp_path):
    store = evidence()
    raw = tmp_path / "evidence.json"
    raw.write_text('{"value":1}')
    artifact = store.describe_evidence(tmp_path, "evidence.json")
    original = run(artifacts=[artifact])
    path = store.persist_run(tmp_path, original)
    raw.write_text('{"value":2}')
    with pytest.raises(ValueError, match="integrity|digest"):
        store.load_run(tmp_path, original.id, original.spec)
    raw.write_text('{"value":1}')
    payload = json.loads(path.read_text())
    payload["output"]["value"] = 9
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest"):
        store.load_run(tmp_path, original.id, original.spec)


def test_receipt_path_and_evidence_paths_cannot_escape_or_follow_symlinks(tmp_path):
    store = evidence()
    source = tmp_path / "original"
    source.mkdir()
    root = tmp_path / "task"
    root.mkdir()
    (root / "numerical").symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|unsafe"):
        store.persist_run(root, run())
    assert list(source.iterdir()) == []
    with pytest.raises(ValueError, match="identifier|pattern"):
        store.load_run(root, "../outside", run().spec)
    with pytest.raises(ValueError, match="relative|traversal"):
        store.describe_evidence(root, "../original")


def test_receipt_identity_and_artifact_bounds_are_verified(tmp_path):
    store = evidence()
    original = run()
    path = store.persist_run(tmp_path, original)
    payload = json.loads(path.read_text())
    payload["id"] = "2" * 32
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        store.load_run(tmp_path, original.id, original.spec)
