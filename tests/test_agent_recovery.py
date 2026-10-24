from pathlib import Path

import pytest

from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.runtime.evidence import agent_receipt_name
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import analysis_plan_data, register_rmse
from tests.test_agent_runtime import confirm, propose, runtime_module, setup


def restarted(runtime, store, tools=None):
    return runtime_module().AgentExecutor(
        ConversationStore(store.root), tools or runtime.tools, runtime.resources
    )


def test_crash_after_receipt_publication_recovers_original_id_without_operation(
    tmp_path, monkeypatch
):
    runtime, store, conversation, calls = setup(tmp_path)
    module = runtime_module()
    publish = module.persist_agent_run

    def crash(root, run):
        publish(root, run)
        raise KeyboardInterrupt("Crash before accepting a verified receipt")

    monkeypatch.setattr(module, "persist_agent_run", crash)
    pending = propose(runtime, conversation)
    with pytest.raises(KeyboardInterrupt):
        confirm(runtime, pending)
    original_id = store.get(pending.id).task.calls[0].id
    monkeypatch.setattr(module, "persist_agent_run", publish)
    upgraded = CapabilityRegistry(agent_mode=True)
    register_rmse(upgraded, calls, version="3")
    recovered = restarted(runtime, store, upgraded).resume(pending.id)
    assert recovered.status == "completed", recovered.error
    assert recovered.task.calls[0].id == original_id
    assert recovered.task.analysis_runs[0].id == original_id
    assert recovered.task.analysis_runs[0].spec.capability_version == "1"
    assert len(calls) == 1


def test_unknown_call_blocks_retry_replan_and_switch_without_redispatch(tmp_path):
    calls = []

    def crash(context, inputs):
        calls.append(context.call_id)
        raise KeyboardInterrupt("Unknown after dispatch")

    runtime, store, conversation, _ = setup(tmp_path, operation=crash)
    pending = propose(runtime, conversation)
    with pytest.raises(KeyboardInterrupt):
        confirm(runtime, pending)
    recovery = restarted(runtime, store)
    blocked = recovery.resume(pending.id)
    assert blocked.status == "interrupted"
    assert "unknown" in blocked.error.lower()
    assert len(blocked.task.calls) == 1
    assert len(calls) == 1
    with pytest.raises(ValueError, match="unknown|unresolved|reconcile"):
        recovery.retry(blocked.id, 1, trusted=True)
    with pytest.raises(ValueError, match="unknown|unresolved|reconcile"):
        recovery.propose(blocked, AgentPlan.model_validate(analysis_plan_data()))
    assert len(calls) == 1


def test_explicit_uncertain_error_does_not_become_a_retryable_failure(tmp_path):
    def uncertain(context, inputs):
        raise runtime_module().ExecutionUncertain("Worker terminal state is unknown.")

    runtime, store, conversation, _ = setup(tmp_path, operation=uncertain)
    done = confirm(runtime, propose(runtime, conversation))
    assert done.status == "interrupted"
    assert done.task.calls[0].status == "interrupted"
    assert done.task.pending_specs
    with pytest.raises(ValueError, match="unknown|unresolved|reconcile"):
        runtime.retry(done.id, 1, trusted=True)


def test_known_failure_needs_explicit_retry_and_does_not_refund_budget(tmp_path):
    from flight_agent.runtime.observations import CapabilityOutcome, Observation

    calls = []

    def fail_once(context, inputs):
        calls.append(context.call_id)
        if len(calls) == 1:
            raise RuntimeError("Confirmed local computation failure")
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id, metric_id="rmse", label="RMSE", value=1, unit="m"
                )
            ]
        )

    runtime, store, conversation, _ = setup(tmp_path, operation=fail_once)
    failed = confirm(runtime, propose(runtime, conversation))
    assert failed.status == "failed"
    assert failed.task.calls[0].status == "failed"
    assert restarted(runtime, store).resume(failed.id).status == "failed"
    assert len(calls) == 1
    done = runtime.retry(failed.id, 1, trusted=True)
    assert done.status == "completed", done.error
    assert len(calls) == 2
    assert done.task.budget_state.calls == 2
    assert calls[0] != calls[1]


def test_completed_original_evidence_does_not_require_the_current_tool(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    first = confirm(runtime, propose(runtime, conversation))
    first.status = "interrupted"
    store.save(first)
    empty = CapabilityRegistry(agent_mode=True)
    recovered = restarted(runtime, store, empty).resume(first.id)
    assert recovered.status == "completed", recovered.error
    assert len(calls) == 1
    assert recovered.task.analysis_runs == first.task.analysis_runs


def test_missing_completed_receipt_blocks_recovery_instead_of_recomputing(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    done = confirm(runtime, propose(runtime, conversation))
    run = done.task.analysis_runs[0]
    (Path(done.task.result_directory) / agent_receipt_name(run.id)).unlink()
    done.status = "interrupted"
    store.save(done)
    blocked = restarted(runtime, store).resume(done.id)
    assert blocked.status == "failed"
    assert "receipt" in blocked.error.lower()
    assert len(calls) == 1


def test_changed_confirmed_baseline_is_not_recovered_under_its_old_confirmation(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    done = confirm(runtime, propose(runtime, conversation))
    done.task.baseline.nodes[0].inputs["observed"] = [100, 200]
    done.status = "interrupted"
    store.save(done)
    with pytest.raises(ValueError, match="baseline|confirmation|scope"):
        restarted(runtime, store).resume(done.id)
    assert len(calls) == 1


@pytest.mark.parametrize("tamper", [False, True])
def test_checkpoint_reconciliation_reads_only_original_call_and_verifies_digest(tmp_path, tamper):
    from flight_agent.runtime.capabilities import Capability
    from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
    from flight_agent.runtime.observations import CapabilityOutcome, Observation
    from tests.agent_helpers import RmseInput

    calls = []
    lookups = []

    def operation(context, inputs):
        calls.append(context.call_id)
        context.save_checkpoint({"value": 1.0, "terminal": True})
        raise KeyboardInterrupt("After durable tool completion checkpoint")

    def reconcile(context, inputs):
        lookups.append(context.call_id)
        data = context.load_checkpoint()
        if data is None:
            return None
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id,
                    metric_id="rmse",
                    label="RMSE",
                    value=data["value"],
                    unit="m",
                )
            ]
        )

    tools = CapabilityRegistry(agent_mode=True)
    tools.register(
        Capability(
            name="offline_rmse",
            version="1",
            description="Reconcile bounded durable measurement.",
            input_model=RmseInput,
            output_model=CapabilityOutcome,
            effect="read_only",
            operation=operation,
            reconcile=reconcile,
            metadata=CapabilityMetadata(
                metrics=[MetricDefinition(id="rmse", label="RMSE", unit="m")]
            ),
        )
    )
    runtime, store, conversation, _ = setup(tmp_path, tools=tools)
    pending = propose(runtime, conversation)
    with pytest.raises(KeyboardInterrupt):
        confirm(runtime, pending)
    if tamper:
        saved = store.get(pending.id).task
        checkpoint = Path(saved.result_directory) / saved.checkpoints[calls[0]]
        checkpoint.write_text('{"value":999}')
    recovered = restarted(runtime, store).resume(pending.id)
    if tamper:
        assert recovered.status == "interrupted"
        assert "digest" in recovered.error.lower()
        assert len(calls) == 1
        assert recovered.task.pending_specs
        return
    assert recovered.status == "completed", recovered.error
    assert lookups == calls
    assert len(calls) == 1
    assert recovered.task.budget_state.calls == 1


def test_receipt_publication_error_stays_unknown_until_original_id_is_reconciled(
    tmp_path, monkeypatch
):
    runtime, store, conversation, calls = setup(tmp_path)
    module = runtime_module()
    publish = module.persist_agent_run

    def interrupted_publish(root, run):
        publish(root, run)
        raise OSError("Storage acknowledgement was lost after publication")

    monkeypatch.setattr(module, "persist_agent_run", interrupted_publish)
    pending = propose(runtime, conversation)
    blocked = confirm(runtime, pending)
    assert blocked.status == "interrupted"
    assert blocked.task.pending_specs
    with pytest.raises(ValueError, match="unknown|unresolved|reconcile"):
        runtime.retry(blocked.id, 1, trusted=True)
    monkeypatch.setattr(module, "persist_agent_run", publish)
    recovered = restarted(runtime, store).resume(blocked.id)
    assert recovered.status == "completed", recovered.error
    assert len(calls) == 1
    assert recovered.task.calls[0].id == blocked.task.calls[0].id


def test_artifact_reuse_copies_verified_bytes_and_preserves_original_receipt(tmp_path):
    from flight_agent.runtime.evidence import describe_evidence, publish_evidence
    from flight_agent.runtime.observations import CapabilityOutcome, OutputArtifact

    calls = []

    def publish(context, inputs):
        calls.append(context.call_id)
        publish_evidence(context.directory, "data/value.json", b'{"value":1}')
        return CapabilityOutcome(
            artifacts=[
                OutputArtifact(
                    **describe_evidence(context.directory, "data/value.json").model_dump(),
                    id="raw",
                    node_id=context.node_id,
                )
            ]
        )

    runtime, store, conversation, _ = setup(tmp_path, operation=publish)
    data = analysis_plan_data()
    data["requirements"]["requested_outputs"] = [
        {
            "id": "raw",
            "kind": "artifact",
            "node_id": "compute",
            "item_id": "raw",
            "description": "Persist raw measurement evidence.",
        }
    ]
    first = confirm(runtime, propose(runtime, conversation, data))
    original = first.task.analysis_runs[0]
    before = (Path(first.task.result_directory) / agent_receipt_name(original.id)).read_bytes()
    second = confirm(runtime, propose(runtime, store.get(first.id), data))
    assert second.status == "completed", second.error
    assert len(calls) == 1
    assert (Path(second.task.result_directory) / "data/value.json").read_bytes() == b'{"value":1}'
    assert (
        Path(first.task.result_directory) / agent_receipt_name(original.id)
    ).read_bytes() == before
    assert second.task.analysis_runs[0].reused_from == original.id
    (Path(first.task.result_directory) / "data/value.json").write_bytes(b"tampered")
    third = confirm(runtime, propose(runtime, store.get(second.id), data))
    assert third.status == "failed"
    assert "integrity" in third.error.lower()
    assert len(calls) == 1


def test_recovery_accounts_for_progress_when_crash_follows_receipt_acceptance(
    tmp_path, monkeypatch
):
    from flight_agent.runtime.observations import CapabilityOutcome, Observation

    calls = []

    def operation(context, inputs):
        calls.append(context.node_id)
        if context.node_id == "compute":
            return CapabilityOutcome()
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id, metric_id="rmse", label="RMSE", value=1, unit="m"
                )
            ]
        )

    runtime, store, conversation, _ = setup(tmp_path, operation=operation)
    accept = runtime._accept

    def crash(conversation, task, node, call, run):
        accept(conversation, task, node, call, run)
        if node.id == "next":
            raise KeyboardInterrupt("Crash after receipt acceptance, before progress checkpoint")

    monkeypatch.setattr(runtime, "_accept", crash)
    data = analysis_plan_data()
    data["nodes"].append({**data["nodes"][0], "id": "next", "depends_on": ["compute"]})
    data["requirements"]["requested_outputs"][0]["node_id"] = "next"
    pending = propose(runtime, conversation, data)
    with pytest.raises(KeyboardInterrupt):
        confirm(runtime, pending)
    assert store.get(pending.id).task.budget_state.no_progress == 1
    recovered = restarted(runtime, store).resume(pending.id)
    assert recovered.status == "completed", recovered.error
    assert calls == ["compute", "next"]
    assert recovered.task.budget_state.no_progress == 0


def test_oversized_serialized_checkpoint_is_rejected_before_publication(tmp_path):
    from flight_agent.runtime.observations import CapabilityOutcome

    def operation(context, inputs):
        context.save_checkpoint({"values": {f"{index:04}": "x" * 430 for index in range(9000)}})
        return CapabilityOutcome()

    runtime, _, conversation, _ = setup(tmp_path, operation=operation)
    done = confirm(runtime, propose(runtime, conversation))
    assert done.status == "failed"
    assert "checkpoint" in done.error.lower() and "size" in done.error.lower()
    assert done.task.checkpoints == {}
    assert list(Path(done.task.result_directory).rglob("checkpoint.json")) == []
