import importlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from flight_agent.runtime.agent_contracts import AgentScope, BudgetState
from flight_agent.runtime.budget import BudgetAccount
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.runtime.contracts import ResourceBudget
from flight_agent.runtime.evidence import publish_evidence
from flight_agent.runtime.observations import CapabilityOutcome, OutputArtifact
from flight_agent.runtime.resources import ResourceReference
from flight_agent.verification.agent_runtime import (
    AgentContext,
    ExecutionUncertain,
    ReconciledFailure,
)
from flight_agent.verification.local_matlab import LocalMatlabBackend
from flight_agent.verification.matlab_contracts import StepinfoInput
from tests.matlab_project_helpers import prepared_project


class RecordingBackend(LocalMatlabBackend):
    """Real local lifecycle with the subprocess's explicitly fake Engine only."""

    def __init__(self):
        super().__init__(sys.executable)
        self.requests = []
        self.lose_ack = False
        self.checkpoint = lambda: None

    def execute(self, request, directory, *, resume=False):
        assert self.checkpoint()["request"] == request.model_dump(mode="json")
        self.requests.append((request, resume))
        assert resume or not directory.exists()
        result = super().execute(request, directory, resume=resume)
        if self.lose_ack and not resume:
            raise OSError("Test-only lost acknowledgement")
        return result


@pytest.fixture
def local_adapter_case(tmp_path, monkeypatch):
    from flight_agent.verification.matlab_projects import discover_matlab_project

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [
                str(Path(__file__).parent / "fixtures" / "fake_local_matlab"),
                str(Path(__file__).resolve().parents[1] / "src"),
            ]
        ),
    )
    project = discover_matlab_project(str(prepared_project(tmp_path / "source")))
    resource = ResourceReference(
        id="local_project",
        kind="matlab_project",
        location="agent",
        locator=project.root,
        fingerprint=project.fingerprint,
        version="1",
        metadata={"project": project.model_dump(mode="json")},
    )
    directory = tmp_path / "task"
    directory.mkdir()
    saved = {}
    context = AgentContext(
        directory=directory,
        scope=AgentScope(
            baseline_version=1,
            confirmed=True,
            allowed_effects=["read_only", "execute_project"],
            resources=[resource],
        ),
        resources=(resource,),
        node_id="execute",
        call_id="a" * 32,
        outputs={},
        budget=BudgetAccount(ResourceBudget(), BudgetState(), lambda state: None),
        load_checkpoint=lambda: saved.get("checkpoint"),
        save_checkpoint=lambda value: saved.update(checkpoint=value),
        _resource_roles={"project": resource},
    )
    backend = RecordingBackend()
    backend.checkpoint = context.load_checkpoint
    tools = CapabilityRegistry(agent_mode=True)
    module = importlib.import_module("flight_agent.verification.adapters.local_matlab")
    for capability in module.local_matlab_capabilities(backend):
        tools.register(capability)
    inputs = {
        "mode": "entry",
        "entry": project.entries[0].model_dump(mode="json"),
        "input_signal": {"source": "workspace", "path": ["command"], "unit": "deg"},
        "response_signal": {"source": "workspace", "path": ["response"], "unit": "deg/s"},
        "conditions": {"flight_case": "cruise", "shape": [None, [], [7]]},
    }
    return context, inputs, backend, tools


def metric_case(case, band=0.05):
    context, inputs, backend, tools = case
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_execute_project", inputs, context, context.scope)
    )
    saved = {}
    metric_context = replace(
        context,
        node_id="measure",
        call_id="b" * 32,
        outputs={"execute": output},
        load_checkpoint=lambda: saved.get("checkpoint"),
        save_checkpoint=lambda value: saved.update(checkpoint=value),
        _resource_roles={},
    )
    backend.checkpoint = metric_context.load_checkpoint
    return (
        metric_context,
        {
            "series": output.details["series"],
            "settling_band": band,
            "conditions": inputs["conditions"],
        },
        backend,
        tools,
    )


def test_execution_publishes_series_with_actual_producer_and_call(local_adapter_case):
    context, inputs, backend, tools = local_adapter_case
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_execute_project", inputs, context, context.scope)
    )
    series = OutputArtifact.model_validate(output.details["series"])
    assert series in output.artifacts
    assert series.node_id == "execute" and series.id == "series"
    assert series.path.startswith("calls/" + "a" * 32 + "/")
    assert all(item.node_id == "execute" for item in output.artifacts)
    assert output.observations == []
    request, resume = backend.requests[0]
    assert request.id == context.call_id and not resume
    assert request.project.fingerprint == context.resource("project").fingerprint
    assert context.load_checkpoint()["request"] == request.model_dump(mode="json")


def test_entry_without_paired_selectors_retains_only_real_nonmetric_outputs(
    local_adapter_case, monkeypatch
):
    context, inputs, backend, tools = local_adapter_case
    inputs.pop("input_signal")
    inputs.pop("response_signal")
    monkeypatch.setenv("TEST_LOCAL_MODE", "raw_only")
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_execute_project", inputs, context, context.scope)
    )
    assert output.observations == [] and "series" not in output.details
    assert "raw" in {item.id for item in output.artifacts}


@pytest.mark.parametrize("band", [0.05, 0.02])
def test_stepinfo_band_and_source_are_forwarded(local_adapter_case, band):
    context, inputs, backend, tools = metric_case(local_adapter_case, band)
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_stepinfo", inputs, context, context.scope)
    )
    request, resume = backend.requests[-1]
    assert request.operation == "stepinfo" and isinstance(request.inputs, StepinfoInput)
    assert request.inputs.settling_band == band and not resume
    assert request.inputs.series.model_dump(mode="json") == inputs["series"]
    assert request.input_evidence == [request.inputs.series]
    assert output.details["settling_band"] == band
    assert {(item.metric_id, item.value, item.unit) for item in output.observations} == {
        ("overshoot", 12.5, "%"),
        ("settling_time", 3.25, "s"),
    }
    assert all(item.conditions == inputs["conditions"] for item in output.observations)
    assert all(item.valid and item.evidence for item in output.observations)
    assert {"step-response.png", "raw.mat", "metadata.json", "runtime-metadata.json"} <= {
        Path(item.path).name for item in output.artifacts
    }
    metadata = json.loads(
        next(
            (context.directory / item.path).read_text()
            for item in output.artifacts
            if Path(item.path).name == "metadata.json"
        )
    )
    assert metadata["inputs"] == request.inputs.model_dump(mode="json")
    assert metadata["conditions"] == inputs["conditions"]
    assert metadata["input_provenance"] == "canonical_request"
    assert output.details["series"] in [item.model_dump(mode="json") for item in output.artifacts]


@pytest.mark.parametrize(
    "change", ["source", "entry", "signature", "resource", "version", "permission", "flag"]
)
def test_execution_refuses_unconfirmed_or_changed_binding(local_adapter_case, monkeypatch, change):
    context, inputs, backend, tools = local_adapter_case
    resource = context.resource("project")
    if change == "source":
        (Path(resource.locator) / "initialize.m").write_text("changed = 1;")
    elif change == "entry":
        inputs["entry"]["path"] = "unselected.m"
    elif change == "signature":
        inputs["entry"]["signature"] = "function x = invented()"
    elif change in {"resource", "version"}:
        resource = resource.model_copy(
            update=({"id": "other"} if change == "resource" else {"version": "2"})
        )
        context = replace(context, _resource_roles={"project": resource})
    elif change == "permission":
        context = replace(
            context, scope=context.scope.model_copy(update={"allowed_effects": ["read_only"]})
        )
    else:
        monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
    with pytest.raises(ValueError):
        tools.invoke("matlab_execute_project", inputs, context, context.scope)
    assert backend.requests == []


@pytest.mark.parametrize(
    "damage",
    [
        "array",
        "forged",
        "producer",
        "call",
        "hash",
        "size",
        "changed",
        "symlink",
        "not_series",
        "conditions",
    ],
)
def test_stepinfo_requires_verified_direct_dependency(local_adapter_case, damage):
    context, inputs, backend, tools = metric_case(local_adapter_case)
    original = inputs["series"].copy()
    if damage == "array":
        inputs["series"] = {"input": [1, 2], "response": [3, 4]}
    elif damage == "forged":
        inputs["series"]["path"] = "forged/series.json"
        publish_evidence(
            context.directory,
            inputs["series"]["path"],
            (context.directory / original["path"]).read_bytes(),
        )
    elif damage == "producer":
        inputs["series"]["node_id"] = "unrelated"
    elif damage == "call":
        inputs["series"]["path"] = original["path"].replace("a" * 32, "c" * 32)
    elif damage == "hash":
        inputs["series"]["sha256"] = "0" * 64
    elif damage == "size":
        inputs["series"]["size"] += 1
    elif damage == "changed":
        (context.directory / original["path"]).write_text("{}")
    elif damage == "symlink":
        source = context.directory / original["path"]
        content = source.read_bytes()
        source.unlink()
        target = context.directory / "outside.json"
        target.write_bytes(content)
        source.symlink_to(target)
    elif damage == "not_series":
        context = replace(
            context,
            outputs={"execute": context.outputs["execute"].model_copy(update={"details": {}})},
        )
    else:
        inputs["conditions"] = {"invented": True}
    with pytest.raises(ValueError):
        tools.invoke("matlab_stepinfo", inputs, context, context.scope)
    assert len(backend.requests) == 1


def test_stepinfo_rechecks_original_bytes_after_calculation(local_adapter_case, monkeypatch):
    context, inputs, backend, tools = metric_case(local_adapter_case)
    execute = backend.execute

    def changed(request, directory, *, resume=False):
        result = execute(request, directory, resume=resume)
        (context.directory / inputs["series"]["path"]).write_text("{}")
        return result

    monkeypatch.setattr(backend, "execute", changed)
    with pytest.raises(ExecutionUncertain, match="integrity|changed"):
        tools.invoke("matlab_stepinfo", inputs, context, context.scope)


@pytest.mark.parametrize("operation", ["matlab_execute_project", "matlab_stepinfo"])
def test_lost_ack_reconciles_original_request_offline(local_adapter_case, monkeypatch, operation):
    case = metric_case(local_adapter_case) if operation == "matlab_stepinfo" else local_adapter_case
    context, inputs, backend, tools = case
    backend.lose_ack = True
    with pytest.raises(ExecutionUncertain):
        tools.invoke(operation, inputs, context, context.scope)
    original = backend.requests[-1][0]
    assert context.load_checkpoint()["request"] == original.model_dump(mode="json")
    with pytest.raises(ExecutionUncertain):
        tools.invoke(operation, inputs, context, context.scope)
    (Path(local_adapter_case[0].resource("project").locator) / "initialize.m").unlink()
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
    output = tools.reconcile(operation, inputs, context, context.scope)
    assert output is not None
    assert backend.requests[-2:] == [(original, False), (original, True)]


@pytest.mark.parametrize("mode", ["startup_error", "missing_receipt"])
def test_known_failure_and_unknown_remain_distinct(local_adapter_case, monkeypatch, mode):
    context, inputs, backend, tools = local_adapter_case
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    error = ValueError if mode == "startup_error" else ExecutionUncertain
    with pytest.raises(error):
        tools.invoke("matlab_execute_project", inputs, context, context.scope)
    error = ReconciledFailure if mode == "startup_error" else ExecutionUncertain
    with pytest.raises(error):
        tools.reconcile("matlab_execute_project", inputs, context, context.scope)
    assert [resume for _, resume in backend.requests] == [False, True]


def test_completed_invalid_metric_retains_evidence_without_valid_observations(
    local_adapter_case, monkeypatch
):
    context, inputs, backend, tools = metric_case(local_adapter_case)
    monkeypatch.setenv("TEST_LOCAL_MODE", "invalid_step")
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_stepinfo", inputs, context, context.scope)
    )
    assert output.details["valid"] is False
    assert output.observations == []
    assert output.findings[0].status == "invalid"
    assert "steady" in output.findings[0].text
    assert {"series.json", "step-response.png", "raw.mat"} <= {
        Path(item.path).name for item in output.artifacts
    }
    from flight_agent.runtime.requirements import Criterion
    from flight_agent.verification.adapters.evaluation import evaluate_criteria

    evaluated = evaluate_criteria(
        [
            Criterion(
                id="settles",
                metric={"node_id": "measure", "metric_id": "settling_time"},
                operator="<",
                threshold=100,
                unit="s",
                conditions=inputs["conditions"],
                source={
                    "kind": "user_message",
                    "reference": "Synthetic regression",
                    "quote": "settling time < 100 s",
                },
            )
        ],
        output.observations,
        overall_requested=True,
    )
    assert evaluated.status == "undetermined"


def test_published_bytes_must_match_verified_backend_descriptor(local_adapter_case, monkeypatch):
    context, inputs, backend, tools = local_adapter_case
    module = importlib.import_module("flight_agent.verification.adapters.local_matlab")
    publish = module.publish_evidence

    def changed_bytes(root, name, content):
        if root == context.directory and name.endswith("/published/series.json"):
            content += b"\n"
        return publish(root, name, content)

    monkeypatch.setattr(module, "publish_evidence", changed_bytes)
    with pytest.raises(ExecutionUncertain, match="integrity"):
        tools.invoke("matlab_execute_project", inputs, context, context.scope)


@pytest.mark.parametrize("band", [0, 1, -0.05, float("nan"), float("inf"), True, "0.05"])
def test_stepinfo_refuses_nonfinite_or_nonexplicit_band(local_adapter_case, band):
    context, inputs, backend, tools = metric_case(local_adapter_case)
    inputs["settling_band"] = band
    with pytest.raises(ValueError):
        tools.invoke("matlab_stepinfo", inputs, context, context.scope)
    assert len(backend.requests) == 1
