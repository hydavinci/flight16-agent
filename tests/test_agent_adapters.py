import importlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import analysis_plan_data, register_rmse
from tests.diagnosis_helpers import AdaptivePlanner, diagnostic_project
from tests.test_verification_runtime import ControlledBackend
from tests.verification_helpers import project_tree


def plugins():
    return importlib.import_module("flight_agent.verification.plugins")


def assembled(tmp_path, backend=None, planner=None):
    tools = CapabilityRegistry(agent_mode=True)
    resources = ResourceRegistry()
    plugins().register_builtin_plugins(
        tools, resources, backend=backend, planner=planner or AdaptivePlanner()
    )
    store = ConversationStore(tmp_path / "state")
    return AgentExecutor(store, tools, resources), store


def simulink_plan(resource, tool="simulink_step_response", metric="overshoot"):
    data = analysis_plan_data()
    data["resources"] = [resource.model_dump(mode="json")]
    data["requirements"]["requested_outputs"][0].update(item_id=metric)
    data["nodes"][0].update(
        tool=tool, inputs={"trim_point": 1}, resource_bindings={"project": resource.id}
    )
    data["allowed_effects"] = ["read_only", "execute_project"]
    return AgentPlan.model_validate(data)


def test_current_simulink_tool_rejects_unsupported_trim_without_dispatch():
    module = importlib.import_module("flight_agent.verification.adapters.simulink")
    backend = ControlledBackend()
    step = module.simulink_capabilities(backend)[0]
    with pytest.raises(ValidationError):
        step.input_model.model_validate({"trim_point": 2})
    assert not backend.calls


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_simulink_trim_rejects_non_integer_representations(value):
    module = importlib.import_module("flight_agent.verification.adapters.simulink")
    with pytest.raises(ValidationError):
        module.SimulinkInput.model_validate({"trim_point": value})


def test_registered_resource_locations_inspect_and_browse_without_execution(tmp_path):
    runtime, _ = assembled(tmp_path, ControlledBackend())
    root = project_tree(tmp_path / "project")
    resource = runtime.resources.inspect("simulink_project", str(root))
    assert resource.kind == "simulink_project"
    assert resource.location == "worker"
    assert resource.locator == str(root)
    runtime.resources.verify([resource])
    listing = runtime.resources.browse("diagnostic_data", str(tmp_path))
    assert root.name in {item.name for item in listing.directories}
    locations = runtime.resources.locations()
    assert len({item.id for item in locations}) == len(locations)
    assert all(item.display_name for item in locations)


def test_planner_receives_project_owned_simulink_configuration_without_execution(tmp_path):
    from flight_agent.verification.agent_planner import AgentPlanner
    from tests.helpers import tool_message
    from tests.test_agent_planner import CapturingModel

    backend = ControlledBackend()
    runtime, store = assembled(tmp_path, backend)
    resource = runtime.resources.inspect(
        "simulink_project", str(project_tree(tmp_path / "project"))
    )
    model = CapturingModel(responses=[
        tool_message("propose_agent", {"action": "clarify", "text": "Test boundary only."})
    ])
    AgentPlanner(model=model).propose(
        "Analyze trim point 1 using the prepared project; no acceptance.",
        store.create(),
        catalog=runtime.tools.catalog((resource,)),
        resources={resource.id: resource},
        sources={},
    )
    context = json.loads(model._seen[0][-1].content)
    catalog = {item["name"]: item for item in context["catalog"]}
    step = catalog["simulink_step_response"]
    policy = step["metadata"]["validity_policy"]
    assert policy["configuration_sources"] == {
        "simulation_settings": "initialized_model",
        "signals": "resource.metadata.config.input_signal/response_signal",
        "steady_state": "resource.metadata.config.steady_state",
        "step": "single_change_in_logged_input",
    }
    assert policy["metric_definitions"]["settling_time"] == {
        "settling_band": 0.05,
        "time_origin": "detected_step",
        "initial_reference": "mean_pre_step_response",
        "final_reference": "mean_verified_steady_state_tail",
        "method": "stepinfo",
    }
    margin = catalog["simulink_stability_margins"]["metadata"]["validity_policy"]
    assert margin["configuration_sources"] == {
        "linearization_points": "getlinio(model)",
        "operating_point": "resource.metadata.config.operating_point_mode",
        "feedback_sign": "resource.metadata.config.feedback_sign",
    }
    assert margin["metric_definitions"] == {
        "gain_margin": {"method": "minimum_20_log10_allmargin_gain_margins"},
        "phase_margin": {"method": "minimum_allmargin_phase_margins"},
    }
    config = context["resources"][0]["metadata"]["config"]
    assert config["input_signal"] == {"name": "stick", "unit": "lb"}
    assert config["response_signal"] == {"name": "response", "unit": "deg/s"}
    assert config["operating_point_mode"] == "initialized"
    assert set(step["input_schema"]["properties"]) == {"trim_point"}
    assert str(resource.locator) not in str(context)
    assert backend.calls == []


def test_registered_resource_rejects_fingerprint_change_before_confirmation(tmp_path):
    backend = ControlledBackend()
    runtime, store = assembled(tmp_path, backend)
    root = project_tree(tmp_path / "project")
    resource = runtime.resources.inspect("simulink_project", str(root))
    pending = runtime.propose(store.create(), simulink_plan(resource))
    (root / "model.slx").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed|fingerprint"):
        runtime.confirm(pending.id, 1, trusted=True)
    assert not backend.calls
    assert not store.get(pending.id).task.baseline.confirmed


@pytest.mark.parametrize(
    "tool,kind,metric,value,unit",
    [
        ("simulink_step_response", "step_response", "overshoot", 5.0, "%"),
        ("simulink_stability_margins", "stability_margins", "gain_margin", 6.0, "dB"),
    ],
)
def test_analysis_uses_bound_project_and_preserves_real_values_without_acceptance(
    tmp_path, tool, kind, metric, value, unit
):
    backend = ControlledBackend()
    runtime, store = assembled(tmp_path, backend)
    root = project_tree(tmp_path / "project")
    resource = runtime.resources.inspect("simulink_project", str(root))
    pending = runtime.propose(store.create(), simulink_plan(resource, tool, metric))
    assert not backend.calls
    completed = runtime.confirm(pending.id, 1, trusted=True)
    assert completed.status == "completed", completed.error
    request, resume = backend.calls[0]
    assert request.id == completed.task.calls[0].id
    assert request.analyses == [kind] and not resume
    assert request.project_root == str(root)
    assert request.project_fingerprint == resource.fingerprint
    assert 1 <= request.timeout_seconds <= 60
    outcome = completed.task.outputs["compute"]
    observation = next(item for item in outcome.observations if item.metric_id == metric)
    assert observation.value == value and observation.unit == unit
    assert observation.conditions["trim_point"] == 1
    assert observation.valid and observation.evidence
    assert outcome.evaluation is None
    assert completed.task.baseline.requirements.criteria == []
    assert all(
        (Path(completed.task.result_directory) / item.path).is_file() for item in outcome.artifacts
    )


def test_simulink_requires_effect_permission_even_with_confirmation(tmp_path):
    backend = ControlledBackend()
    runtime, store = assembled(tmp_path, backend)
    resource = runtime.resources.inspect(
        "simulink_project", str(project_tree(tmp_path / "project"))
    )
    data = simulink_plan(resource).model_dump(mode="json")
    data["allowed_effects"] = ["read_only"]
    with pytest.raises(ValueError, match="effect|permission"):
        runtime.propose(store.create(), AgentPlan.model_validate(data))
    assert not backend.calls


def test_lost_worker_acknowledgement_reconciles_exact_original_request(tmp_path):
    backend = ControlledBackend()
    backend.fail_after_completion = True
    runtime, store = assembled(tmp_path, backend)
    resource = runtime.resources.inspect(
        "simulink_project", str(project_tree(tmp_path / "project"))
    )
    pending = runtime.propose(store.create(), simulink_plan(resource))
    failed = runtime.confirm(pending.id, 1, trusted=True)
    assert failed.status == "interrupted"
    original = backend.calls[0][0]
    restored, _ = assembled(tmp_path, backend)
    done = restored.resume(failed.id)
    assert done.status == "completed", done.error
    assert backend.calls == [(original, False), (original, True)]
    assert done.task.calls[0].id == original.id
    assert len(done.task.analysis_runs) == 1


def test_missing_worker_does_not_block_offline_registered_tools(tmp_path):
    runtime, store = assembled(tmp_path)
    root = diagnostic_project(tmp_path / "diagnostic")
    data = runtime.resources.inspect("diagnostic_data", str(root))
    assert data.kind == "diagnostic_data" and data.location == "agent"
    runtime.resources.verify([data])
    locations = {item.id: item for item in runtime.resources.locations()}
    assert locations["simulink_project"].availability.state == "unavailable"
    assert locations["diagnostic_data"].availability.state == "available"
    with pytest.raises(ValueError, match="Worker|worker"):
        runtime.resources.inspect("simulink_project", str(root))
    register_rmse(runtime.tools, [])
    pending = runtime.propose(store.create(), AgentPlan.model_validate(analysis_plan_data()))
    completed = runtime.confirm(pending.id, 1, trusted=True)
    assert completed.status == "completed", completed.error
    assert completed.task.result.observations[0].value == pytest.approx(2**0.5)


def test_resource_adapters_reject_duplicate_registration_and_unknown_names(tmp_path):
    runtime, _ = assembled(tmp_path)
    with pytest.raises(ValueError, match="registered"):
        plugins().register_builtin_plugins(
            runtime.tools, runtime.resources, backend=None, planner=AdaptivePlanner()
        )
    with pytest.raises(ValueError, match="registered"):
        runtime.resources.inspect("model_generated_import", str(tmp_path))


class ResultBackend(ControlledBackend):
    def __init__(self, transform, *, rewrite_record=True):
        super().__init__()
        self.transform = transform
        self.rewrite_record = rewrite_record

    def execute(self, request, directory, on_progress, *, resume=False):
        result = super().execute(request, directory, on_progress, resume=resume)
        data = result.model_dump(mode="json")
        self.transform(data)
        result = type(result).model_validate(data)
        if self.rewrite_record:
            (directory / "analysis.json").write_text(result.model_dump_json())
        return result


def test_invalid_worker_metric_retains_value_reason_and_provenance(tmp_path):
    def invalid(data):
        data["analyses"][0].update(
            valid=False,
            diagnostics=["Steady state is not established."],
            details={"stop_time": "12", "solver": "ode45"},
        )
        data["provenance"] = {"selection": {"altitude_ft": 5000}}

    runtime, store = assembled(tmp_path, ResultBackend(invalid))
    resource = runtime.resources.inspect(
        "simulink_project", str(project_tree(tmp_path / "project"))
    )
    pending = runtime.propose(store.create(), simulink_plan(resource))
    done = runtime.confirm(pending.id, 1, trusted=True)
    assert done.status == "completed", done.error
    output = done.task.outputs["compute"]
    overshoot = next(item for item in output.observations if item.metric_id == "overshoot")
    assert overshoot.value == 5.0 and not overshoot.valid
    assert overshoot.reason == "Steady state is not established."
    record = output.details["analysis_result"]
    assert record["provenance"]["selection"]["altitude_ft"] == 5000
    assert record["analyses"][0]["details"]["solver"] == "ode45"


def test_worker_result_must_match_persisted_analysis_record(tmp_path):
    backend = ResultBackend(
        lambda data: data["analyses"][0]["metrics"]["overshoot"].update(value=99.0),
        rewrite_record=False,
    )
    runtime, store = assembled(tmp_path, backend)
    resource = runtime.resources.inspect(
        "simulink_project", str(project_tree(tmp_path / "project"))
    )
    pending = runtime.propose(store.create(), simulink_plan(resource))
    done = runtime.confirm(pending.id, 1, trusted=True)
    assert done.status == "interrupted"
    assert not done.task.analysis_runs
    assert done.task.outputs == {}


def test_simulink_evaluates_only_the_single_user_requested_metric(tmp_path):
    runtime, store = assembled(tmp_path, ControlledBackend())
    resource = runtime.resources.inspect(
        "simulink_project", str(project_tree(tmp_path / "project"))
    )
    data = simulink_plan(resource).model_dump(mode="json")
    criterion = {
        "id": "custom_overshoot",
        "metric": {"node_id": "compute", "metric_id": "overshoot"},
        "operator": "<=",
        "threshold": 7.25,
        "unit": "%",
        "conditions": {"trim_point": 1},
        "source": {
            "kind": "user_message",
            "reference": "user_clause",
            "quote": "At trim point 1 require overshoot <= 7.25%.",
        },
    }
    data["requirements"].update(mode="verification", criteria=[criterion], overall_requested=False)
    data["nodes"].append(
        {
            "id": "evaluate",
            "tool": "evaluate_criteria",
            "tool_version": "1",
            "depends_on": ["compute"],
            "inputs": {"criteria": [criterion], "overall_requested": False},
        }
    )
    data["evaluation_node"] = "evaluate"
    pending = runtime.propose(store.create(), AgentPlan.model_validate(data))
    done = runtime.confirm(pending.id, 1, trusted=True)
    assert done.status == "completed", done.error
    assert {item.metric_id for item in done.task.result.observations} == {
        "overshoot",
        "settling_time",
    }
    evaluation = done.task.result.evaluation
    assert evaluation.status is None
    assert len(evaluation.checks) == 1
    assert evaluation.checks[0].criterion_id == "custom_overshoot"
    assert evaluation.checks[0].threshold == 7.25
