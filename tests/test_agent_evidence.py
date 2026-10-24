import json

import pytest

from flight_agent.runtime import evidence
from flight_agent.runtime.agent_contracts import InvocationRun, InvocationSpec
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.observations import CapabilityOutcome, OutputArtifact
from tests.test_runtime_evidence import run as legacy_run


def run(**changes):
    output = CapabilityOutcome(details={"value": 1.0})
    values = {
        "id": "1" * 32,
        "spec": InvocationSpec(
            capability="offline_rmse",
            capability_version="1",
            inputs={"observed": [1.0], "reference": [0.0]},
        ),
        "output": output,
        "output_sha256": fingerprint(output.model_dump(mode="json")),
        "started_at": "2026-10-22T00:00:00Z",
        "finished_at": "2026-10-22T00:00:01Z",
        **changes,
    }
    return InvocationRun.model_validate(values)


def test_v2_receipt_is_immutable_idempotent_and_has_no_forced_design_pair(tmp_path):
    original = run()
    path = evidence.persist_agent_run(tmp_path, original)
    assert path == tmp_path / f"agent/analysis-runs/{original.id}.json"
    assert evidence.load_agent_run(tmp_path, original.id, original.spec) == original
    assert evidence.persist_agent_run(tmp_path, original) == path
    output = CapabilityOutcome(details={"value": 2.0})
    changed = run(output=output, output_sha256=fingerprint(output.model_dump(mode="json")))
    with pytest.raises(ValueError, match="immutable"):
        evidence.persist_agent_run(tmp_path, changed)
    assert evidence.load_agent_run(tmp_path, original.id, original.spec) == original
    assert evidence.load_agent_run(tmp_path, "2" * 32, original.spec) is None


@pytest.mark.parametrize("mutation", ["digest", "identity", "spec"])
def test_v2_receipts_cannot_be_reassigned_or_have_outputs_rewritten(tmp_path, mutation):
    original = run()
    path = evidence.persist_agent_run(tmp_path, original)
    if mutation == "spec":
        spec = original.spec.model_copy(update={"inputs": {"observed": [100]}})
        with pytest.raises(ValueError, match="spec"):
            evidence.load_agent_run(tmp_path, original.id, spec)
        return
    payload = json.loads(path.read_text("utf-8"))
    if mutation == "digest":
        payload["output"]["details"]["value"] = 99
    else:
        payload["id"] = "2" * 32
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest|identity"):
        evidence.load_agent_run(tmp_path, original.id, original.spec)


@pytest.mark.parametrize("envelope_only", [False, True])
def test_all_v2_artifacts_are_verified_including_envelope_metadata(tmp_path, envelope_only):
    artifact_path = tmp_path / "metric.json"
    artifact_path.write_text('{"value": 1}')
    descriptor = evidence.describe_evidence(tmp_path, "metric.json")
    artifact = OutputArtifact(**descriptor.model_dump(), id="metric-record", node_id="compute")
    output = CapabilityOutcome(artifacts=[artifact])
    original = run(
        output=output,
        output_sha256=fingerprint(output.model_dump(mode="json")),
        artifacts=[] if envelope_only else [artifact],
    )
    evidence.persist_agent_run(tmp_path, original)
    assert evidence.load_agent_run(tmp_path, original.id, original.spec) == original
    artifact_path.write_text('{"value": 2}')
    with pytest.raises(ValueError, match="integrity|digest"):
        evidence.load_agent_run(tmp_path, original.id, original.spec)


def test_v1_and_v2_receipts_with_same_id_remain_separate_and_unchanged(tmp_path):
    legacy = legacy_run()
    old_path = evidence.persist_run(tmp_path, legacy)
    before = old_path.read_bytes()
    new = run()
    evidence.persist_agent_run(tmp_path, new)
    assert old_path.read_bytes() == before
    assert evidence.load_run(tmp_path, legacy.id, legacy.spec) == legacy
    assert evidence.load_agent_run(tmp_path, new.id, new.spec) == new


@pytest.mark.parametrize("operation", ["read", "write"])
def test_v2_receipt_symlink_parents_are_rejected(tmp_path, operation):
    target = tmp_path / "outside"
    target.mkdir()
    directory = tmp_path / "task"
    directory.mkdir()
    (directory / "agent").symlink_to(target, target_is_directory=True)
    original = run()
    with pytest.raises(ValueError, match="symlink"):
        if operation == "read":
            evidence.load_agent_run(directory, original.id, original.spec)
        else:
            evidence.persist_agent_run(directory, original)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("run_id", ["../outside", "1" * 31, "1" * 32 + "/x"])
def test_v2_receipt_ids_cannot_escape_the_task_directory(tmp_path, run_id):
    with pytest.raises(ValueError):
        evidence.load_agent_run(tmp_path, run_id, run().spec)


def test_v2_receipt_read_and_write_size_limits_preserve_existing_data(tmp_path, monkeypatch):
    original = run()
    path = evidence.persist_agent_run(tmp_path, original)
    content = path.read_bytes()
    monkeypatch.setattr(evidence, "MAX_RECEIPT_BYTES", 10)
    with pytest.raises(ValueError, match="size|oversized"):
        evidence.load_agent_run(tmp_path, original.id, original.spec)
    with pytest.raises(ValueError, match="size|oversized"):
        evidence.persist_agent_run(tmp_path, run(id="2" * 32))
    assert path.read_bytes() == content
    assert not (path.parent / f"{'2' * 32}.json").exists()


def test_forged_v2_run_cannot_bypass_output_validation(tmp_path):
    forged = run().model_copy(update={"output_sha256": "b" * 64})
    with pytest.raises(ValueError, match="digest"):
        evidence.persist_agent_run(tmp_path, forged)
    assert list(tmp_path.iterdir()) == []
