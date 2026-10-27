import json
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import Field, ValidationError

from flight_agent.models import Contract, FiniteNumber
from tests.test_verification_runtime import confirm, proposal, runtime_fixture
from tests.verification_helpers import verification_module


class PeakInput(Contract):
    limit: Annotated[FiniteNumber, Field(ge=0)]


class PeakOutput(Contract):
    peak: FiniteNumber
    acceptable: bool


def extension_plan():
    plan = proposal()
    plan["nodes"].insert(
        1,
        {
            "id": "check",
            "tool": "check_peak",
            "depends_on": ["analyze"],
            "inputs": {"limit": 6.0},
        },
    )
    plan["nodes"][2]["depends_on"] = ["check"]
    return plan


def peak_tool(operation=None, **overrides):
    tools = verification_module("tools")

    def check(context, inputs):
        peak = context.result.analyses[0].metrics["overshoot"].value
        return PeakOutput(peak=peak, acceptable=peak <= inputs.limit)

    settings = {
        "name": "check_peak",
        "description": "Check the recorded overshoot without executing source code.",
        "input_model": PeakInput,
        "output_model": PeakOutput,
        "requires": frozenset({"numerical_result"}),
        "provides": frozenset({"peak_check"}),
        "operation": operation or check,
    }
    settings.update(overrides)
    return tools.ReadOnlyTool(**settings)


def test_registered_typed_analysis_executes_and_is_recorded_without_runtime_changes(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path, [extension_plan()])
    observations = []

    def check(context, inputs):
        assert isinstance(inputs, PeakInput)
        assert not hasattr(context, "task")
        assert not hasattr(context, "project")
        peak = context.result.analyses[0].metrics["overshoot"].value
        observations.append(context.read_artifact(context.result.artifacts[0].path))
        assert set(context.outputs) == {"analyze"}
        context.result.analyses.clear()
        context.baseline.analyses.clear()
        context.outputs.clear()
        return PeakOutput(peak=peak, acceptable=peak <= inputs.limit)

    runtime.tools.register(peak_tool(check))
    pending = runtime.submit(None, "Also check the recorded peak against 6%.", str(root))
    assert pending.pending_decision.kind == "confirm_baseline", pending.error
    completed = confirm(runtime, pending)
    assert completed.status == "completed", completed.error
    assert len(backend.calls) == 1
    assert observations and completed.task.result.analyses
    assert completed.task.baseline.analyses == ["step_response"]
    call = next(call for call in completed.task.calls if call.tool == "check_peak")
    assert call.output == {"peak": 5.0, "acceptable": True}
    assert call.inputs["limit"] == 6.0
    directory = Path(completed.task.result_directory)
    record = json.loads((directory / "delivery-v1/execution_record.json").read_text())
    assert any(
        item["tool"] == "check_peak" and item["output"] == call.output for item in record["calls"]
    )
    assert any(item.path.endswith(".docx") for item in completed.task.artifacts)
    context = json.loads(model.seen[0][-1])
    definition = next(item for item in context["tools"] if item["name"] == "check_peak")
    assert definition["permission"] == "read_only"
    assert "limit" in definition["input_schema"]["properties"]
    assert "acceptable" in definition["output_schema"]["properties"]


def test_declared_tool_version_is_pinned_and_persisted_with_its_output(tmp_path):
    runtime, _, model, root = runtime_fixture(tmp_path, [extension_plan()])
    runtime.tools.register(peak_tool(version="2.3"))
    pending = runtime.submit(None, "Verify with the registered extension.", str(root))
    assert pending.task.proposal.tool_versions["check_peak"] == "2.3"
    definition = next(
        item for item in json.loads(model.seen[0][-1])["tools"] if item["name"] == "check_peak"
    )
    assert definition["version"] == "2.3"
    completed = confirm(runtime, pending)
    assert completed.status == "completed", completed.error
    call = next(item for item in completed.task.calls if item.tool == "check_peak")
    assert call.tool_version == "2.3"
    restored = runtime.store.get(completed.id)
    assert next(item for item in restored.task.calls if item.id == call.id).tool_version == "2.3"
    record = json.loads(
        (Path(completed.task.result_directory) / "delivery-v1/execution_record.json").read_text()
    )
    assert next(item for item in record["calls"] if item["id"] == call.id)["tool_version"] == "2.3"


def test_changed_pending_tool_version_cannot_execute_under_old_confirmation(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [extension_plan()])
    runtime.tools.register(peak_tool(version="2"))
    pending = runtime.submit(None, "Verify.", str(root))
    restored = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    invoked = []
    restored.tools.register(peak_tool(
        lambda context, inputs: invoked.append(inputs), version="3"
    ))
    rejected = confirm(restored, pending)
    assert rejected.status == "waiting_for_user"
    assert "version" in rejected.error.lower()
    assert not rejected.task.baseline.confirmed
    assert not backend.calls and not invoked


def test_completed_extension_retains_original_version_after_an_implementation_upgrade(
    tmp_path, monkeypatch
):
    module = verification_module("runtime")
    original = module.deliver_report
    runtime, backend, _, root = runtime_fixture(tmp_path, [extension_plan()])
    runtime.tools.register(peak_tool(version="2"))

    def fail_delivery(*args, **kwargs):
        raise OSError("Controlled report failure")

    monkeypatch.setattr(module, "deliver_report", fail_delivery)
    failed = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    assert failed.status == "failed"
    monkeypatch.setattr(module, "deliver_report", original)
    restored = module.VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    invoked = []
    restored.tools.register(peak_tool(
        lambda context, inputs: invoked.append(inputs), version="3"
    ))
    done = restored.resume(failed.id)
    assert done.status == "completed", done.error
    assert not invoked
    assert next(item for item in done.task.calls if item.tool == "check_peak").tool_version == "2"
    assert done.task.proposal.tool_versions["check_peak"] == "2"


def test_legacy_tool_call_does_not_invent_an_implementation_version():
    call = verification_module("contracts").ToolCall(
        id="a" * 32, node_id="check", tool="check_peak", baseline_version=1,
        started_at="2026-10-20T12:00:00+00:00",
    )
    assert call.tool_version is None
    assert call.model_dump()["tool_version"] is None


@pytest.mark.parametrize(
    "nodes,error",
    [
        ([{"id": "same", "tool": "analyze_project"}] * 2, "unique|duplicate"),
        ([{"id": "Bad id", "tool": "analyze_project"}], "pattern|identifier"),
        ([{"id": "a", "tool": "arbitrary/code"}], "pattern|identifier"),
        ([{"id": "a", "tool": "analyze_project", "depends_on": ["absent"]}], "unknown"),
        ([{"id": "a", "tool": "analyze_project", "depends_on": ["a"]}], "cycle"),
        (
            [
                {"id": "a", "tool": "analyze_project", "depends_on": ["b"]},
                {"id": "b", "tool": "evaluate_results", "depends_on": ["a"]},
            ],
            "cycle",
        ),
        ([], "at least|nonempty|minimum"),
    ],
)
def test_plan_contract_rejects_invalid_dependency_graphs(nodes, error):
    data = proposal()
    data["nodes"] = nodes
    with pytest.raises(ValidationError, match=error):
        verification_module("contracts").PlanProposal.model_validate(data)


@pytest.mark.parametrize(
    "invalid", ["unknown", "input", "flow", "late_analysis", "duplicate_resource"]
)
def test_registry_rejects_unavailable_or_unsafe_plans_before_confirmation(tmp_path, invalid):
    plan = extension_plan()
    if invalid == "unknown":
        plan["nodes"][1]["tool"] = "not_installed"
    elif invalid == "input":
        plan["nodes"][1]["inputs"] = {"limit": -1}
    elif invalid == "flow":
        plan["nodes"][1]["depends_on"] = []
    elif invalid == "late_analysis":
        plan["nodes"][2]["depends_on"] = ["analyze"]
        plan["nodes"][3]["depends_on"] = ["evaluate", "check"]
    else:
        plan["nodes"].insert(1, {"id": "again", "tool": "analyze_project"})
    runtime, backend, _, root = runtime_fixture(tmp_path, [plan])
    runtime.tools.register(peak_tool())
    rejected = runtime.submit(None, "Verify.", str(root))
    assert rejected.task is None
    assert rejected.error
    assert rejected.pending_decision.kind == "clarify"
    assert not backend.calls


def test_registry_rejects_overrides_and_privilege_escalation(tmp_path):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    runtime.tools.register(peak_tool())
    with pytest.raises(ValueError, match="registered|duplicate"):
        runtime.tools.register(peak_tool())
    with pytest.raises(ValueError, match="registered|reserved"):
        runtime.tools.register(peak_tool(name="analyze_project"))
    for resource in ("numerical_result", "evaluation", "delivery"):
        with pytest.raises(ValueError, match="reserved|capability"):
            runtime.tools.register(peak_tool(name="other_check", provides=frozenset({resource})))
    with pytest.raises((TypeError, ValueError), match="permission|read.only"):
        runtime.tools.register(peak_tool(name="execute_source", permission="execute"))


@pytest.mark.parametrize("bad_output", ["shape", "nonfinite", "constructed"])
def test_invalid_tool_output_blocks_evaluation_and_delivery(tmp_path, bad_output):
    def check(context, inputs):
        if bad_output == "shape":
            return {"acceptable": True}
        if bad_output == "nonfinite":
            return {"peak": float("nan"), "acceptable": True}
        return PeakOutput.model_construct(peak="not a number", acceptable=True)

    runtime, backend, _, root = runtime_fixture(tmp_path, [extension_plan()])
    runtime.tools.register(peak_tool(check))
    failed = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    assert failed.status == "failed"
    assert next(node for node in failed.task.nodes if node.id == "check").status == "failed"
    assert failed.error
    assert failed.task.evaluation is None
    assert not any(item.role == "report" for item in failed.task.artifacts)
    assert failed.task.nodes[-1].status == "pending"
    assert len(backend.calls) == 1


def test_registered_artifact_reader_rejects_unlisted_and_tampered_files(tmp_path):
    def check(context, inputs):
        with pytest.raises(ValueError, match="registered"):
            context.read_artifact("not-registered.txt")
        path = Path(runtime.store.get(conversation_id).task.result_directory)
        artifact = context.result.artifacts[0]
        (path / artifact.path).write_bytes(b"tampered")
        context.read_artifact(artifact.path)
        return PeakOutput(peak=5.0, acceptable=True)

    runtime, backend, _, root = runtime_fixture(tmp_path, [extension_plan()])
    runtime.tools.register(peak_tool(check))
    pending = runtime.submit(None, "Verify.", str(root))
    conversation_id = pending.id
    failed = confirm(runtime, pending)
    assert failed.status == "failed"
    assert failed.task.evaluation is None
    assert len(backend.calls) == 1
    assert "artifact" in failed.error.lower()


@pytest.mark.parametrize("installed", [True, False])
def test_restart_checks_registry_and_reuses_completed_extension_work(
    tmp_path, monkeypatch, installed
):
    module = verification_module("runtime")
    original = module.deliver_report
    calls = []

    def check(context, inputs):
        calls.append(inputs.limit)
        return PeakOutput(peak=5.0, acceptable=True)

    def fail_delivery(*args, **kwargs):
        raise OSError("Controlled report failure")

    runtime, backend, _, root = runtime_fixture(tmp_path, [extension_plan()])
    runtime.tools.register(peak_tool(check))
    monkeypatch.setattr(module, "deliver_report", fail_delivery)
    failed = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    assert failed.status == "failed"
    assert calls == [6.0]
    monkeypatch.setattr(module, "deliver_report", original)
    restored = module.VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    if installed:
        restored.tools.register(peak_tool(check))
    result = restored.resume(failed.id)
    assert len(backend.calls) == 1
    assert calls == [6.0]
    if installed:
        assert result.status == "completed", result.error
        assert any(item.path.endswith(".docx") for item in result.task.artifacts)
    else:
        assert result.status == "failed"
        assert "check_peak" in result.error
        assert "registered" in result.error.lower() or "available" in result.error.lower()
