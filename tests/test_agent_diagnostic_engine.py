import importlib
from pathlib import Path

import pytest

from flight_agent.runtime.agent_contracts import AgentPlan, BudgetState
from flight_agent.runtime.budget import BudgetAccount
from flight_agent.runtime.contracts import ResourceBudget
from flight_agent.runtime.evidence import verify_evidence
from flight_agent.verification.contracts import AgentTask, DiagnosisTask
from flight_agent.verification.diagnostic_data import diagnostic_capabilities
from tests.agent_helpers import analysis_plan_data
from tests.diagnosis_helpers import AdaptivePlanner, EvolvingPlanner, diagnostic_project
from tests.test_agent_adapters import assembled
from tests.test_diagnosis_contracts import diagnosis_proposal


def diagnosis_plan(resource, *, budget=None):
    data = analysis_plan_data()
    proposal = diagnosis_proposal()
    versions = {
        name: diagnostic_capabilities().definition(name).version
        for name in proposal["allowed_capabilities"]
    }
    data["resources"] = [resource.model_dump(mode="json")]
    data["requirements"]["requested_outputs"] = [
        {
            "id": "summary",
            "kind": "finding",
            "node_id": "compute",
            "item_id": "diagnosis_summary",
            "description": "Evidence-backed diagnostic summary.",
        }
    ]
    data["nodes"][0].update(
        tool="offline_diagnosis",
        inputs={"proposal": proposal, "capability_versions": versions},
        resource_bindings={"diagnostic_data": resource.id},
    )
    data["budget"] = budget or {
        "max_rounds": 6,
        "max_calls": 12,
        "max_seconds": 300,
        "max_no_progress": 2,
    }
    return data


def proposed_diagnosis(tmp_path, *, planner=None, budget=None):
    runtime, store = assembled(tmp_path, planner=planner)
    root = diagnostic_project(tmp_path / "data")
    resource = runtime.resources.inspect("diagnostic_data", str(root))
    data = diagnosis_plan(resource, budget=budget)
    pending = runtime.propose(store.create(), AgentPlan.model_validate(data))
    return runtime, store, pending, data


def test_generic_diagnosis_preserves_outer_task_and_two_evidence_rounds(tmp_path):
    planner = EvolvingPlanner()
    runtime, store, pending, _ = proposed_diagnosis(tmp_path, planner=planner)
    assert not planner.seen
    done = runtime.confirm(pending.id, 1, trusted=True)
    assert done.status == "completed", done.error
    assert isinstance(done.task, AgentTask)
    assert len(done.task.analysis_runs) == 1
    outcome = done.task.outputs["compute"]
    summary = next(item for item in outcome.findings if item.id == "diagnosis_summary")
    assert summary.status == "undetermined" and summary.evidence
    assert any(item.id == "alignment" and item.status == "unknown" for item in outcome.findings)
    assert outcome.evaluation is None
    assert len(planner.seen) == 2
    internal = [item for item in outcome.artifacts if "/analysis-runs/" in item.path]
    assert len(internal) == 2
    assert all(verify_evidence(Path(done.task.result_directory), item) for item in internal)
    checkpoint = store.get(done.id).task.checkpoints
    assert checkpoint
    assert done.task.budget_state.calls == 5  # outer, two analyses, two reviews
    assert done.task.budget_state.rounds == 3
    assert planner.seen[0][0]["spec"]["design"]["controller"]["sha256"] == "a" * 64


@pytest.mark.parametrize("max_calls,expected_runs", [(1, 0), (2, 1), (3, 1)])
def test_outer_budget_bounds_every_internal_analysis_and_review(tmp_path, max_calls, expected_runs):
    planner = AdaptivePlanner()
    budget = {"max_rounds": 6, "max_calls": max_calls, "max_seconds": 300, "max_no_progress": 2}
    runtime, _, pending, _ = proposed_diagnosis(tmp_path, planner=planner, budget=budget)
    done = runtime.confirm(pending.id, 1, trusted=True)
    assert done.status == "completed", done.error
    output = done.task.outputs["compute"]
    assert output.details["termination"]["reason"] == "call_budget"
    assert (
        len([item for item in output.artifacts if "/analysis-runs/" in item.path]) == expected_runs
    )
    assert done.task.budget_state.calls == max_calls
    assert len(planner.seen) == (1 if max_calls == 3 else 0)
    assert output.evaluation is None


def test_diagnosis_checkpoint_recovers_original_inner_receipt_without_reexecution(
    tmp_path, monkeypatch
):
    module = importlib.import_module("flight_agent.verification.diagnostic_engine")
    original = module.persist_run
    writes = []

    def lost_ack(directory, run):
        original(directory, run)
        writes.append(run.id)
        if len(writes) == 1:
            raise OSError("Controlled lost diagnostic receipt acknowledgement")

    monkeypatch.setattr(module, "persist_run", lost_ack)
    runtime, store, pending, _ = proposed_diagnosis(tmp_path)
    first = runtime.confirm(pending.id, 1, trusted=True)
    assert first.status == "interrupted", first.error
    assert len(writes) == 1
    restored, _ = assembled(tmp_path)
    done = restored.resume(first.id)
    assert done.status == "completed", done.error
    assert len(writes) == 2 and len(set(writes)) == 2
    assert store.get(first.id).task.calls[0].id == first.task.calls[0].id


def test_unknown_internal_execution_never_redispatches_on_resume(tmp_path, monkeypatch):
    module = importlib.import_module("flight_agent.verification.adapters.diagnostics")
    original = module.diagnostic_capabilities
    dispatched = []

    def interrupted_tools():
        tools = original()

        def interrupted(*args, **kwargs):
            dispatched.append(True)
            raise KeyboardInterrupt("Controlled interruption without a receipt")

        monkeypatch.setattr(tools, "invoke", interrupted)
        return tools

    monkeypatch.setattr(module, "diagnostic_capabilities", interrupted_tools)
    runtime, _, pending, _ = proposed_diagnosis(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        runtime.confirm(pending.id, 1, trusted=True)
    restored, _ = assembled(tmp_path)
    done = restored.resume(pending.id)
    assert done.status == "interrupted", done.error
    assert dispatched == [True]
    assert not done.task.analysis_runs


def test_internal_capability_version_change_is_rejected_before_execution(tmp_path):
    runtime, store = assembled(tmp_path)
    resource = runtime.resources.inspect(
        "diagnostic_data", str(diagnostic_project(tmp_path / "data"))
    )
    data = diagnosis_plan(resource)
    data["nodes"][0]["inputs"]["capability_versions"]["compare_signals"] = "upgraded"
    with pytest.raises(ValueError, match="version|changed"):
        runtime.propose(store.create(), AgentPlan.model_validate(data))


def test_same_engine_runs_without_a_conversation_or_legacy_delivery(tmp_path):
    module = importlib.import_module("flight_agent.verification.diagnostic_engine")
    runtime, store, pending, _ = proposed_diagnosis(tmp_path)
    from flight_agent.verification.diagnosis import DiagnosisRuntime

    legacy = DiagnosisRuntime(store, AdaptivePlanner())
    conversation = store.create()
    conversation.project_root = pending.task.baseline.resources[0].locator
    legacy.propose(conversation, "Investigate supplied records.")
    task = conversation.task
    assert isinstance(task, DiagnosisTask)
    task.baseline = task.baseline.model_copy(update={"confirmed": True})
    checkpoints = []
    account = BudgetAccount(ResourceBudget(), BudgetState(), lambda state: None)
    completed = module.DiagnosticEngine(diagnostic_capabilities(), AdaptivePlanner()).run(
        task,
        checkpoint=lambda value: checkpoints.append(value.model_dump(mode="json")),
        budget=account,
    )
    assert completed.termination.reason == "evidence_sufficient"
    assert len(completed.analysis_runs) == 2
    assert any(item["pending_analysis"] for item in checkpoints)
    assert account.state.calls == 4
    assert not list(Path(task.result_directory).glob("*.docx"))


def test_bounded_planner_passes_remaining_timeout_without_mutating_original():
    from pydantic import Field

    from flight_agent.verification.diagnosis_contracts import DiagnosisProposal
    from flight_agent.verification.planner import StructuredPlanner
    from tests.helpers import ScriptedModel, tool_message

    class TimedModel(ScriptedModel):
        timeouts: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.timeouts.append(kwargs.get("timeout"))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = TimedModel(
        responses=[
            tool_message("proposal", diagnosis_proposal()),
            tool_message("proposal", diagnosis_proposal()),
        ]
    )
    planner = StructuredPlanner(model)
    bounded = planner.with_timeout(1.25)
    bounded.call("proposal", DiagnosisProposal, {}, system_prompt="test")
    planner.call("proposal", DiagnosisProposal, {}, system_prompt="test")
    assert model.timeouts == [1.25, None]


def test_recovery_does_not_charge_the_same_internal_round_twice(tmp_path, monkeypatch):
    module = importlib.import_module("flight_agent.verification.diagnostic_engine")
    original = module.persist_run
    interrupted = []

    def lost_ack(directory, run):
        original(directory, run)
        if not interrupted:
            interrupted.append(run.id)
            raise OSError("Controlled lost acknowledgement in the only allowed round")

    monkeypatch.setattr(module, "persist_run", lost_ack)
    budget = {"max_rounds": 2, "max_calls": 12, "max_seconds": 300, "max_no_progress": 2}
    runtime, _, pending, _ = proposed_diagnosis(tmp_path, budget=budget)
    first = runtime.confirm(pending.id, 1, trusted=True)
    assert first.status == "interrupted"
    restored, _ = assembled(tmp_path)
    done = restored.resume(first.id)
    assert done.status == "completed", done.error
    assert done.task.budget_state.rounds == 2
    assert done.task.budget_state.calls == 3
    assert len(done.task.outputs["compute"].details["internal_run_ids"]) == 1
    assert done.task.outputs["compute"].details["termination"]["reason"] == "round_budget"


def test_shared_no_progress_limit_stops_repeated_internal_cache_rounds(tmp_path):
    planner = AdaptivePlanner(repeat=True)
    budget = {"max_rounds": 6, "max_calls": 12, "max_seconds": 300, "max_no_progress": 1}
    runtime, _, pending, _ = proposed_diagnosis(tmp_path, planner=planner, budget=budget)
    done = runtime.confirm(pending.id, 1, trusted=True)
    assert done.status == "completed", done.error
    assert len(planner.seen) == 2
    assert done.task.outputs["compute"].details["termination"]["reason"] == "no_progress"
    assert len(done.task.outputs["compute"].details["internal_run_ids"]) == 1


def test_generic_diagnosis_planning_exposes_registered_inner_input_schemas(tmp_path):
    import json

    runtime, _ = assembled(tmp_path)
    snapshot = runtime.tools.snapshot("offline_diagnosis")
    declarations = json.loads(snapshot.metadata.planning_notes.split("\n", 1)[1])
    actual = {item["name"]: item for item in declarations}
    assert set(actual) == {
        "window_statistics",
        "compare_signals",
        "parameter_sensitivity",
        "local_loop",
    }
    assert actual["compare_signals"]["version"] == "1"
    fields = actual["compare_signals"]["input_schema"]["properties"]
    assert set(fields) == {
        "start",
        "end",
        "observed_dataset",
        "observed_signal",
        "reference_dataset",
        "reference_signal",
        "alignment",
        "reference_time_offset",
    }


def test_interruption_between_round_debit_and_checkpoint_does_not_spend_another_round(
    tmp_path, monkeypatch
):
    original = BudgetAccount.charge_round
    interrupted = []

    def crash_after_debit(account, *args, **kwargs):
        original(account, *args, **kwargs)
        if account.state.rounds == 2 and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("Crash after durable debit, before diagnostic checkpoint")

    monkeypatch.setattr(BudgetAccount, "charge_round", crash_after_debit)
    budget = {"max_rounds": 2, "max_calls": 12, "max_seconds": 300, "max_no_progress": 2}
    runtime, _, pending, _ = proposed_diagnosis(tmp_path, budget=budget)
    with pytest.raises(KeyboardInterrupt):
        runtime.confirm(pending.id, 1, trusted=True)
    restored, _ = assembled(tmp_path)
    done = restored.resume(pending.id)
    assert done.status == "completed", done.error
    output = done.task.outputs["compute"]
    assert len(output.details["internal_run_ids"]) == 1
    assert output.details["termination"]["reason"] == "round_budget"
    assert done.task.budget_state.rounds == 2


@pytest.mark.parametrize("source_state", ["changed", "unavailable"])
def test_reconciliation_uses_original_snapshot_when_live_source_is_not_available(
    tmp_path, monkeypatch, source_state
):
    module = importlib.import_module("flight_agent.verification.diagnostic_engine")
    original = module.persist_run
    interrupted = []

    def lost_ack(directory, run):
        original(directory, run)
        if not interrupted:
            interrupted.append(run.id)
            raise OSError("Lost acknowledgement before source moves or changes")

    monkeypatch.setattr(module, "persist_run", lost_ack)
    runtime, store, pending, _ = proposed_diagnosis(tmp_path)
    first = runtime.confirm(pending.id, 1, trusted=True)
    assert first.status == "interrupted"
    source = Path(first.task.baseline.resources[0].locator)
    if source_state == "changed":
        (source / "observed.json").write_text('{"changed":true}')
    else:
        source.rename(tmp_path / "moved-source")
    restored, _ = assembled(tmp_path)
    done = restored.resume(first.id)
    assert done.status == "completed", done.error
    assert store.get(first.id).task.calls[0].id == first.task.calls[0].id
    assert done.task.outputs["compute"].details["internal_run_ids"][0] == interrupted[0]
