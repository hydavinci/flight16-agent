import json

import pytest
from pydantic import Field

from flight_agent.models import Contract, FiniteNumber
from tests.test_runtime_kernel import design_data, kernel
from tests.verification_helpers import verification_module


class MassInput(Contract):
    components: list[FiniteNumber] = Field(min_length=1, max_length=20)


class MassOutput(Contract):
    total: FiniteNumber
    unit: str


def registry(calls, *, version="2.3", operation=None, effect="read_only"):
    capabilities = kernel("capabilities")
    tools = capabilities.CapabilityRegistry()

    def measure(context, inputs):
        calls.append(inputs.components)
        return MassOutput(total=sum(inputs.components), unit="kg")

    tools.register(
        capabilities.Capability(
            name="mass_summary", version=version,
            description="Summarize a supplied synthetic component mass list.",
            input_model=MassInput, output_model=MassOutput,
            effect=effect, operation=operation or measure,
        )
    )
    return tools


def proposal(**changes):
    values = {
        "title": "Synthetic mass summary",
        "rationale": "Sum the explicit components without accessing an engineering project.",
        "design": design_data(),
        "nodes": [
            {"id": "mass", "tool": "mass_summary", "inputs": {"components": [2.0, 3.5]}}
        ],
    }
    values.update(changes)
    return kernel("tasks").GeneralProposal.model_validate(values)


def setup(tmp_path, **options):
    store = verification_module("store").ConversationStore(tmp_path / "state")
    calls = []
    runtime = verification_module("general").GeneralRuntime(store, registry(calls, **options))
    conversation = store.create()
    return runtime, store, conversation, calls


def test_registered_third_main_capability_has_its_own_schema_and_durable_version(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    assert planned.pending_decision.kind == "confirm_baseline"
    assert not planned.task.baseline.confirmed
    assert len(planned.task.nodes) == 1
    assert not calls
    assert not hasattr(planned.task.baseline, "trim_point")
    assert not hasattr(planned.task.baseline, "analyses")

    done = runtime.confirm(planned, planned.task)
    assert done.status == "completed"
    assert done.task.outputs == {"mass": {"total": 5.5, "unit": "kg"}}
    assert calls == [[2.0, 3.5]]
    assert done.task.calls[0].tool_version == "2.3"
    saved = store.get(conversation.id)
    assert saved.task.scenario == "general"
    assert saved.task.workflow == "mass_summary"
    assert saved.task.analysis_runs[0].spec.capability_version == "2.3"
    for artifact in saved.task.artifacts:
        path = verification_module("artifacts").verify_artifact(
            store.task_directory(saved.id, saved.task.id), artifact
        )
        if artifact.path == "execution_record.json":
            record = json.loads(path.read_text("utf-8"))
            assert record["calls"][0]["tool_version"] == "2.3"
    assert any(item.path == "execution_record.json" for item in saved.task.artifacts)


def test_changed_pending_version_cannot_be_confirmed_or_dispatched(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    replacement = verification_module("general").GeneralRuntime(store, registry(calls, version="3"))
    blocked = replacement.confirm(planned, planned.task)
    assert blocked.status == "waiting_for_user"
    assert not blocked.task.baseline.confirmed
    assert "version" in blocked.error.lower()
    assert not calls


def test_unknown_interrupted_call_is_never_redispatched(tmp_path):
    calls = []

    def crash(context, inputs):
        calls.append(inputs.components)
        raise KeyboardInterrupt("Synthetic process termination after dispatch")

    runtime, store, conversation, _ = setup(tmp_path, operation=crash)
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    with pytest.raises(KeyboardInterrupt):
        runtime.confirm(planned, planned.task)
    restarted = verification_module("store").ConversationStore(store.root)
    saved = restarted.get(conversation.id)
    assert saved.status == "interrupted"
    resumed = verification_module("general").GeneralRuntime(restarted, registry(calls))
    blocked = resumed.run(saved, saved.task)
    assert blocked.status == "failed"
    assert "unknown" in blocked.error.lower()
    assert calls == [[2.0, 3.5]]
    assert len(blocked.task.calls) == 1


@pytest.mark.parametrize(
    "inputs,effect,error",
    [
        ({"components": [2.0], "unregistered": True}, "read_only", "Extra inputs"),
        ({"components": [float("inf")]}, "read_only", "finite"),
        ({"components": [2.0]}, "execute_project", "effect|permission"),
    ],
)
def test_plan_validation_precedes_task_creation_and_execution(tmp_path, inputs, effect, error):
    runtime, store, conversation, calls = setup(tmp_path, effect=effect)
    with pytest.raises(ValueError, match=error):
        plan = proposal(nodes=[{"id": "mass", "tool": "mass_summary", "inputs": inputs}])
        runtime.propose(conversation, plan, workflow="mass_summary")
    assert store.get(conversation.id).task is None
    assert not calls


def test_invalid_capability_output_is_failed_not_presented_as_evidence(tmp_path):
    runtime, store, conversation, _ = setup(
        tmp_path, operation=lambda context, inputs: {"total": 5.5, "unit": "kg", "pass": True}
    )
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    failed = runtime.confirm(planned, planned.task)
    assert failed.status == "failed"
    assert "Extra inputs" in failed.error
    assert not failed.task.analysis_runs
    assert not failed.task.outputs
    assert failed.task.calls[0].status == "failed"
    assert store.get(conversation.id).status == "failed"


def test_general_execution_obeys_shared_driver_bound(tmp_path, monkeypatch):
    runtime, _, conversation, calls = setup(tmp_path)
    module = verification_module("general")
    loop = kernel("loop")
    monkeypatch.setattr(
        module, "run_loop",
        lambda **options: loop.run_loop(**{**options, "max_steps": 1}),
    )
    planned = runtime.propose(
        conversation,
        proposal(nodes=[
            {"id": "first", "tool": "mass_summary", "inputs": {"components": [2.0]}},
            {
                "id": "second", "tool": "mass_summary", "depends_on": ["first"],
                "inputs": {"components": [3.5]},
            },
        ]),
        workflow="mass_summary",
    )
    failed = runtime.confirm(planned, planned.task)
    assert failed.status == "failed"
    assert "step budget" in failed.error
    assert failed.task.outputs == {"first": {"total": 2.0, "unit": "kg"}}
    assert calls == [[2.0]]


def test_completed_receipts_survive_a_tool_upgrade_during_delivery_recovery(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    done = runtime.confirm(planned, planned.task)
    done.status = "interrupted"
    store.save(done)
    restarted = verification_module("general").GeneralRuntime(store, registry(calls, version="3"))
    recovered = restarted.run(done, done.task)
    assert recovered.status == "completed"
    assert calls == [[2.0, 3.5]]
    assert recovered.task.calls[0].tool_version == "2.3"
    assert recovered.task.analysis_runs[0].spec.capability_version == "2.3"


def test_unknown_call_with_original_receipt_is_reconciled_without_current_implementation(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    done = runtime.confirm(planned, planned.task)
    call = done.task.calls[0]
    call.status = "interrupted"
    call.output = {}
    call.finished_at = None
    done.task.nodes[0].status = "interrupted"
    done.task.analysis_runs = []
    done.task.outputs = {}
    done.status = "interrupted"
    store.save(done)
    restarted = verification_module("general").GeneralRuntime(store, registry(calls, version="3"))
    recovered = restarted.run(done, done.task)
    assert recovered.status == "completed"
    assert recovered.task.outputs == {"mass": {"total": 5.5, "unit": "kg"}}
    assert recovered.task.calls[0].tool_version == "2.3"
    assert calls == [[2.0, 3.5]]


def test_completed_evidence_cannot_be_reassigned_to_another_confirmed_spec(tmp_path):
    runtime, store, conversation, calls = setup(tmp_path)
    planned = runtime.propose(conversation, proposal(), workflow="mass_summary")
    done = runtime.confirm(planned, planned.task)
    original = done.task.analysis_runs[0]
    changed = original.spec.model_copy(update={"inputs": {"components": [100.0]}})
    forged = kernel("contracts").AnalysisRun(
        **{
            **original.model_dump(), "spec": changed.model_dump(),
            "id": "f" * 32,
        }
    )
    kernel("evidence").persist_run(
        store.task_directory(done.id, done.task.id), forged
    )
    done.task.analysis_runs = [forged]
    done.task.calls[0].id = forged.id
    done.status = "interrupted"
    store.save(done)
    blocked = runtime.run(done, done.task)
    assert blocked.status == "failed"
    assert "baseline" in blocked.error.lower() or "scope" in blocked.error.lower()
    assert calls == [[2.0, 3.5]]


def test_dependent_capability_observes_recorded_output_and_binds_it_to_the_receipt(tmp_path):
    def observe(context, inputs):
        total = sum(inputs.components) + sum(value["total"] for value in context.outputs.values())
        return MassOutput(total=total, unit="kg")

    runtime, _, conversation, _ = setup(tmp_path, operation=observe)
    planned = runtime.propose(
        conversation, proposal(nodes=[
            {"id": "first", "tool": "mass_summary", "inputs": {"components": [2.0]}},
            {
                "id": "second", "tool": "mass_summary", "depends_on": ["first"],
                "inputs": {"components": [3.5]},
            },
        ]), workflow="mass_summary",
    )
    done = runtime.confirm(planned, planned.task)
    assert done.status == "completed"
    assert done.task.outputs["second"] == {"total": 5.5, "unit": "kg"}
    conditions = done.task.analysis_runs[-1].spec.conditions
    assert set(conditions["dependency_output_sha256"]) == {"first"}
