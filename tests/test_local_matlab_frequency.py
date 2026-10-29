"""Software-only frequency-capability regressions; never import a real Engine."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from flight_agent.runtime.agent_contracts import AgentPlanDraft
from flight_agent.runtime.evidence import publish_evidence
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification import matlab_contracts as contracts
from flight_agent.verification.agent_planner import bind_plan
from flight_agent.verification.agent_runtime import ExecutionUncertain
from flight_agent.verification.matlab_projects import discover_matlab_project
from tests import test_local_matlab as backend_tests
from tests import test_local_matlab_adapters as adapter_tests
from tests import test_local_matlab_planning as planning_tests

local_backend = backend_tests.local_backend
local_request = backend_tests.local_request
local_adapter_case = adapter_tests.local_adapter_case
local_planning_case = planning_tests.local_planning_case
local_draft = planning_tests.local_draft
submit = planning_tests.submit

DECLARATIONS = (
    "Existing model: plant.slx\nExisting initializer: initialize.m\n"
    "Operating point: initialized current selected operating point.\n"
    "Use saved preset linearization points from getlinio; do not guess points.\n"
    "Under negative feedback the loop must not be negated; feedback_sign = 1.\n"
    "Preparation: choose_point(pointNumber), before the existing initializer.\n"
    "Selected.PointIndex records the selected point.\n"
    "point_index: 7\n"
)


def selection_source(quote):
    return {"reference": "README.md", "quote": quote}


def linear_inputs(project, point=7):
    return {
        "model": "plant.slx",
        "initializer": next(
            item.model_dump(mode="json") for item in project.entries if item.path == "initialize.m"
        ),
        "operating_point": {
            "mode": "initialized",
            "source": selection_source(
                "Operating point: initialized current selected operating point."
            ),
        },
        "feedback_sign": 1,
        "feedback_source": selection_source(
            "Under negative feedback the loop must not be negated; feedback_sign = 1."
        ),
        "conditions": {"point_index": point},
        "workspace_assertions": [{
            "path": ["Selected", "PointIndex"],
            "condition": "point_index",
            "source": selection_source("Selected.PointIndex records the selected point."),
        }],
    }


def prepare_frequency_project(root):
    (root / "README.md").write_text(DECLARATIONS)
    (root / "choose_point.m").write_text(
        "function selection = choose_point(varargin)\nselection = varargin;\nend\n"
    )
    return discover_matlab_project(str(root))


def linear_request(local_request):
    assert hasattr(contracts, "LinearizeInput"), "Missing typed linearize capability contract"
    project = prepare_frequency_project(Path(local_request.project.root))
    inputs = linear_inputs(project)
    entry = next(item for item in project.entries if item.path == "choose_point.m")
    inputs["preparations"] = [{
        "entry": entry.model_copy(update={"arguments": [7]}).model_dump(mode="json"),
        "argument_conditions": ["point_index"],
        "source": selection_source(
            "Preparation: choose_point(pointNumber), before the existing initializer."
        ),
    }]
    inputs["workspace_assertions"] = [{
        "path": ["Selected", "PointIndex"],
        "condition": "point_index",
        "source": selection_source("Selected.PointIndex records the selected point."),
    }]
    return contracts.LocalMatlabRequest.model_validate({
        "id": "c" * 32,
        "operation": "linearize",
        "inputs": inputs,
        "project": project.model_dump(mode="json"),
    })


def test_linearize_contract_is_explicit_and_preserves_old_request_rules(local_request):
    request = linear_request(local_request)
    assert request.inputs.preparations[0].entry.arguments == [7]
    assert request.inputs.workspace_assertions[0].condition == "point_index"
    for field in ("operating_point", "feedback_sign", "feedback_source"):
        data = request.model_dump(mode="json")
        data["inputs"].pop(field)
        with pytest.raises(ValueError):
            contracts.LocalMatlabRequest.model_validate(data)
    data = request.model_dump(mode="json")
    data["operation"] = "execute_project"
    with pytest.raises(ValueError):
        contracts.LocalMatlabRequest.model_validate(data)
    data = request.model_dump(mode="json")
    data["inputs"]["operating_point"]["path"] = ["x;system('bad')"]
    with pytest.raises(ValueError):
        contracts.LocalMatlabRequest.model_validate(data)
    assert contracts.LocalMatlabRequest.model_validate(local_request) == local_request


def test_linearize_records_runtime_and_preparation_without_shared_workspace(
    local_backend, local_request, tmp_path
):
    request = linear_request(local_request)
    directory = tmp_path / "linear"
    result = local_backend.execute(request, directory)
    assert result.valid, result.diagnostics
    assert result.details["linear_model"]["path"] == "linear-model.mat"
    assert result.details["linearization"]["workspace_assertions"] == [
        {"path": ["Selected", "PointIndex"], "condition": "point_index", "actual": 7}
    ]
    execution = json.loads((directory / "execution.json").read_text())
    assert execution["preparations"][0]["nargin"] == -1
    assert execution["preparations"][0]["arguments"] == ["7"]
    assert execution["inputs"]["initializer"] == request.inputs.initializer.model_dump(mode="json")
    assert local_backend.execute(request, directory, resume=True) == result
    assert (directory / "engine-starts.txt").read_text() == "1"


def frequency_case(case):
    context, _, backend, tools = case
    project = prepare_frequency_project(Path(context.resource("project").locator))
    resource = context.resource("project").model_copy(
        update={
            "fingerprint": project.fingerprint,
            "metadata": {"project": project.model_dump(mode="json")},
        }
    )
    context = replace(
        context,
        resources=(resource,),
        scope=context.scope.model_copy(update={"resources": [resource]}),
        _resource_roles={"project": resource},
    )
    inputs = linear_inputs(project)
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_linearize", inputs, context, context.scope)
    )
    saved = {}
    metric_context = replace(
        context,
        node_id="margins",
        call_id="b" * 32,
        outputs={"execute": output},
        _resource_roles={},
        load_checkpoint=lambda: saved.get("checkpoint"),
        save_checkpoint=lambda value: saved.update(checkpoint=value),
    )
    backend.checkpoint = metric_context.load_checkpoint
    return metric_context, {
        "linear_model": output.details["linear_model"],
        "conditions": inputs["conditions"],
    }, backend, tools


def test_allmargin_publishes_all_crossings_stability_and_infinity(local_adapter_case):
    context, inputs, backend, tools = frequency_case(local_adapter_case)
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_allmargin", inputs, context, context.scope)
    )
    assert [
        (item.metric_id, item.value, item.special, item.unit) for item in output.observations
    ] == [
        ("gain_margin", 6.0, None, "dB"),
        ("phase_margin", None, "positive_infinity", "deg"),
    ]
    assert len(output.details["gain_crossings"]) == 2
    assert output.details["phase_crossings"][0]["margin"]["special"] == "positive_infinity"
    assert output.details["closed_loop_stable"] is True
    assert {"bode.png", "linear-model.mat", "producer-metadata.json", "raw.mat"} <= {
        Path(item.path).name for item in output.artifacts
    }
    request, resume = backend.requests[-1]
    assert request.operation == "allmargin" and request.project is None and not resume
    assert len(request.input_evidence) == 2


@pytest.mark.parametrize("damage", [
    "producer", "conditions", "hash", "changed", "forged", "metadata", "wrong_operation",
])
def test_allmargin_rejects_non_dependency_or_tampered_model(local_adapter_case, damage):
    context, inputs, backend, tools = frequency_case(local_adapter_case)
    artifact = inputs["linear_model"]
    if damage == "producer":
        artifact["node_id"] = "unrelated"
    elif damage == "conditions":
        inputs["conditions"] = {"point_index": 1}
    elif damage == "hash":
        artifact["sha256"] = "0" * 64
    elif damage == "changed":
        (context.directory / artifact["path"]).write_bytes(b"changed")
    elif damage == "forged":
        content = (context.directory / artifact["path"]).read_bytes()
        artifact["path"] = "forged/linear-model.mat"
        publish_evidence(context.directory, artifact["path"], content)
    elif damage == "metadata":
        metadata = next(
            item for item in context.outputs["execute"].artifacts if item.id == "metadata"
        )
        (context.directory / metadata.path).write_text("{}")
    else:
        output = context.outputs["execute"]
        context = replace(context, outputs={"execute": output.model_copy(
            update={"details": {**output.details, "operation": "execute_project"}}
        )})
    with pytest.raises(ValueError):
        tools.invoke("matlab_allmargin", inputs, context, context.scope)
    assert len(backend.requests) == 1


@pytest.mark.parametrize("mode", [
    "wrong_margin_unit", "nan_margin", "inconsistent_crossing", "missing_plot",
    "changed_linear_source", "wrong_nominal_stability",
])
def test_allmargin_bad_bridge_never_becomes_success(local_adapter_case, monkeypatch, mode):
    context, inputs, backend, tools = frequency_case(local_adapter_case)
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    with pytest.raises(ExecutionUncertain):
        tools.invoke("matlab_allmargin", inputs, context, context.scope)
    assert len(backend.requests) == 2


@pytest.mark.parametrize("mode", ["invalid_margin", "non_siso"])
def test_invalid_margins_retain_diagnostics_not_observations(local_adapter_case, monkeypatch, mode):
    context, inputs, backend, tools = frequency_case(local_adapter_case)
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_allmargin", inputs, context, context.scope)
    )
    assert output.details["valid"] is False
    assert output.observations == [] and output.findings[0].status == "invalid"
    assert "bode" in " ".join(item.path for item in output.artifacts)


def test_frequency_lost_ack_reconciles_original_without_reexecution(
    local_adapter_case, monkeypatch,
):
    context, inputs, backend, tools = frequency_case(local_adapter_case)
    backend.lose_ack = True
    with pytest.raises(ExecutionUncertain):
        tools.invoke("matlab_allmargin", inputs, context, context.scope)
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
    assert tools.reconcile("matlab_allmargin", inputs, context, context.scope) is not None
    assert [resume for _, resume in backend.requests] == [False, False, True]
    directory = context.directory / f"calls/{context.call_id}/local-matlab"
    assert (directory / "engine-starts.txt").read_text() == "1"


def frequency_draft(context):
    data = local_draft(context)
    project = contracts.MatlabProjectDescriptor.model_construct(
        entries=[contracts.MatlabEntry.model_validate(item)
                 for item in context["resources"][0]["metadata"]["project"]["entries"]]
    )
    inputs = linear_inputs(project)
    data["nodes"][0].update(tool="matlab_linearize", inputs=inputs)
    data["nodes"][1].update(
        tool="matlab_allmargin",
        inputs={"conditions": inputs["conditions"]},
        bindings={"linear_model": {"node_id": "execute", "path": ["details", "linear_model"]}},
    )
    for criterion, metric, unit in zip(
        data["requirements"]["criteria"],
        ("gain_margin", "phase_margin"), ("dB", "deg"), strict=True,
    ):
        criterion.update(
            id=metric, metric={"node_id": "measure", "metric_id": metric}, unit=unit,
            conditions=inputs["conditions"],
        )
    for output, metric in zip(
        data["requirements"]["requested_outputs"][:2], ("gain_margin", "phase_margin"), strict=True
    ):
        output.update(id=metric, item_id=metric)
    return data


def test_frequency_planning_requires_confirmation_without_engine(local_planning_case):
    (local_planning_case[6] / "README.md").write_text(DECLARATIONS)
    local_planning_case[5]["build"] = frequency_draft
    record = submit(local_planning_case)
    assert record.task is not None, record.messages[-1].content
    assert not record.task.baseline.confirmed
    assert record.pending_decision.kind == "confirm_baseline"
    assert local_planning_case[3].requests == []


@pytest.mark.parametrize("damage", [
    "operating_source", "feedback", "initializer", "conditions", "producer", "binding",
    "criterion_conditions", "resource", "direct_model",
])
def test_frequency_planner_preserves_source_role_and_condition_guards(local_planning_case, damage):
    (local_planning_case[6] / "README.md").write_text(DECLARATIONS)
    local_planning_case[5]["build"] = frequency_draft
    record = submit(local_planning_case)
    assert record.task is not None, record.messages[-1].content
    baseline = record.task.baseline
    data = {
        key: value for key, value in baseline.model_dump(mode="json").items()
        if key in AgentPlanDraft.model_fields
    }
    data["resource_ids"] = [item.id for item in baseline.resources]
    linear, margins = data["nodes"][:2]
    if damage == "operating_source":
        linear["inputs"]["operating_point"]["source"]["quote"] = "invented operating point"
    elif damage == "feedback":
        linear["inputs"]["feedback_sign"] = -1
    elif damage == "initializer":
        linear["inputs"]["initializer"] = None
    elif damage == "conditions":
        margins["inputs"]["conditions"] = {"point_index": 1}
    elif damage == "producer":
        linear["tool"] = "matlab_execute_project"
    elif damage == "binding":
        margins["bindings"]["linear_model"]["path"] = ["artifacts", "0"]
    elif damage == "criterion_conditions":
        data["requirements"]["criteria"][0]["conditions"] = {"point_index": 1}
        data["nodes"][2]["inputs"]["criteria"] = deepcopy(data["requirements"]["criteria"])
    elif damage == "resource":
        linear["resource_bindings"] = {}
    else:
        margins["inputs"]["linear_model"] = {"path": "invented.mat"}
        margins["bindings"] = {}
    runtime = local_planning_case[1]
    with pytest.raises(ValueError):
        bind_plan(
            AgentPlanDraft.model_validate(data),
            {item.id: item for item in baseline.resources},
            {item.source.reference: item.source for item in baseline.requirements.criteria},
            runtime.executor.tools.catalog(tuple(baseline.resources)),
        )


def test_discovery_exposes_bounded_attributed_workspace_sources_without_execution(local_request):
    root = Path(local_request.project.root)
    text = (
        "function selection = choose_point(varargin)\n"
        "selection.PointIndex = varargin{1};\n"
        "assignin('base','Selected',selection);\nend\n"
    )
    (root / "choose_point.m").write_text(text)
    project = discover_matlab_project(str(root))
    assert project.settings.get("workspace_sources") == [{"path": "choose_point.m", "text": text}]


@pytest.mark.parametrize("mode", ["default_point_mismatch", "inactive_points", "wrong_feedback"])
def test_linearization_runtime_selection_mismatch_never_publishes_model(
    local_backend, local_request, tmp_path, monkeypatch, mode
):
    request = linear_request(local_request)
    data = request.model_dump(mode="json")
    data["inputs"]["conditions"] = {"point_index": 1}
    data["inputs"]["preparations"][0]["entry"]["arguments"] = [1]
    request = contracts.LocalMatlabRequest.model_validate(data)
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    directory = tmp_path / "linear-mismatch"
    result = local_backend.execute(request, directory)
    assert not result.valid and result.execution_status == "unknown"
    assert "linear-model.mat" not in {item.path for item in result.artifacts}
    assert local_backend.execute(request, directory, resume=True) == result
    assert (directory / "engine-starts.txt").read_text() == "1"


@pytest.mark.parametrize("operation", ["matlab_linearize", "matlab_allmargin"])
def test_frequency_capabilities_require_confirmation_and_permission(local_adapter_case, operation):
    if operation == "matlab_allmargin":
        context, inputs, backend, tools = frequency_case(local_adapter_case)
    else:
        context, _, backend, tools = local_adapter_case
        inputs = linear_inputs(contracts.MatlabProjectDescriptor.model_validate(
            context.resource("project").metadata["project"]
        ))
    count = len(backend.requests)
    for update in ({"confirmed": False}, {"allowed_effects": ["read_only"]}):
        scope = context.scope.model_copy(update=update)
        with pytest.raises(ValueError):
            tools.invoke(operation, inputs, replace(context, scope=scope), scope)
        assert len(backend.requests) == count


def test_linear_model_damage_invalidates_offline_recovery(local_backend, local_request, tmp_path):
    request = linear_request(local_request)
    directory = tmp_path / "linear"
    assert local_backend.execute(request, directory).valid
    (directory / "linear-model.mat").write_bytes(b"MATLAB 5.0 MAT-file, forged result")
    result = local_backend.execute(request, directory, resume=True)
    assert result.execution_status == "unknown" and not result.valid
    assert "digest" in " ".join(result.diagnostics)
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_sourced_preparation_varargin_and_workspace_assertion_bind_without_defaults(
    local_planning_case,
):
    root = local_planning_case[6]
    prepare_frequency_project(root)

    def build(context):
        data = frequency_draft(context)
        project = context["resources"][0]["metadata"]["project"]
        entry = next(item for item in project["entries"] if item["path"] == "choose_point.m")
        data["nodes"][0]["inputs"].update(
            preparations=[{
                "entry": {**entry, "arguments": [7]},
                "argument_conditions": ["point_index"],
                "source": selection_source(
                    "Preparation: choose_point(pointNumber), before the existing initializer."
                ),
            }],
            workspace_assertions=[{
                "path": ["Selected", "PointIndex"], "condition": "point_index",
                "source": selection_source("Selected.PointIndex records the selected point."),
            }],
        )
        return data

    local_planning_case[5]["build"] = build
    record = submit(local_planning_case)
    assert record.task is not None, record.messages[-1].content
    assert record.task.baseline.nodes[0].inputs["preparations"][0]["entry"]["arguments"] == [7]
    assert local_planning_case[3].requests == []


def test_linearize_checks_embedded_producer_provenance(
    local_backend, local_request, tmp_path, monkeypatch,
):
    request = linear_request(local_request)
    original_spawn = local_backend._spawn

    def corrupt_bridge(argument, *, cwd, log):
        process = original_spawn(argument, cwd=cwd, log=log)
        if argument != "--probe":
            process.wait(timeout=15)
            bridge_path = cwd / "bridge.json"
            bridge = json.loads(bridge_path.read_text())
            bridge["details"]["linearization"]["id"] = "d" * 32
            bridge_path.write_text(json.dumps(bridge))
            receipt_path = cwd / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["details"]["linearization"]["id"] = "d" * 32
            receipt_path.write_text(json.dumps(receipt))
        return process

    monkeypatch.setattr(local_backend, "_spawn", corrupt_bridge)
    result = local_backend.execute(request, tmp_path / "producer-mismatch")
    assert not result.valid and result.execution_status == "unknown"


def test_negative_phase_and_unstable_nominal_loop_remain_valid_failed_criteria(
    local_adapter_case, monkeypatch,
):
    from flight_agent.runtime.requirements import Criterion
    from flight_agent.verification.adapters.evaluation import evaluate_criteria

    context, inputs, backend, tools = frequency_case(local_adapter_case)
    monkeypatch.setenv("TEST_LOCAL_MODE", "negative_phase")
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_allmargin", inputs, context, context.scope)
    )
    assert output.details["valid"] is True and output.details["closed_loop_stable"] is False
    assert len(output.details["phase_crossings"]) == 2
    assert [(item.metric_id, item.value) for item in output.observations] == [
        ("gain_margin", 6.0), ("phase_margin", -5.0),
    ]
    criteria = [
        Criterion(
            id=name, metric={"node_id": "margins", "metric_id": name}, operator=">=",
            threshold=0, unit=unit, conditions=inputs["conditions"],
            source={"kind": "user_message", "reference": "synthetic-only",
                    "quote": "Both margins >= 0 in declared units"},
        )
        for name, unit in (("gain_margin", "dB"), ("phase_margin", "deg"))
    ]
    evaluation = evaluate_criteria(criteria, output.observations, overall_requested=True)
    assert [item.status for item in evaluation.checks] == ["pass", "fail"]
    assert evaluation.status == "fail"


def test_linearization_cannot_relabel_default_operating_point_with_unverified_conditions(
    local_request,
):
    request = linear_request(local_request)
    data = request.model_dump(mode="json")
    data["inputs"]["preparations"] = []
    data["inputs"]["workspace_assertions"] = []
    with pytest.raises(ValueError, match="condition"):
        contracts.LocalMatlabRequest.model_validate(data)


def test_natural_language_condition_source_does_not_require_a_retyped_manifest(local_planning_case):
    root = local_planning_case[6]
    (root / "README.md").write_text(DECLARATIONS.replace("point_index: 7\n", ""))

    def build(context):
        data = frequency_draft(context)
        source = context["sources"][0]
        data["nodes"][0]["inputs"]["condition_sources"] = {
            "point_index": {"reference": source["reference"], "quote": "配平点 7 线性化"}
        }
        return data

    local_planning_case[5]["build"] = build
    record = submit(
        local_planning_case,
        message="配平点 7 线性化；allmargin 评价；gain_margin <=10 dB；"
        "phase_margin <=3 deg；交付 JSON 和 Word。",
    )
    assert record.task is not None, record.messages[-1].content
    assert not record.task.baseline.confirmed
    assert record.task.baseline.nodes[0].inputs["conditions"] == {"point_index": 7}
    assert local_planning_case[3].requests == []


@pytest.mark.parametrize("declaration,sign,accepted", [
    (
        "该组点得到一个输入、一个输出的纵向环路模型。"
        "输入为前馈输出，单位 deg；输出为反馈汇总的升降舵修正量，单位 deg。"
        "前馈在最终求和负号端进入，按负反馈环路约定使用时不需要再取负。",
        1, True,
    ),
    ("按负反馈环路约定，不需要再取负。", -1, False),
    ("按负反馈环路约定，无需再次取负。", 1, True),
    ("按负反馈环路约定，不应再取负。", 1, True),
    ("Under negative feedback, do not negate the loop.", 1, True),
    ("Under negative feedback, there is no need to negate the loop.", 1, True),
    ("Under negative feedback, the loop does not need to be negated.", 1, True),
    ("Under negative feedback, the loop must not be negated.", 1, True),
    ("Under negative feedback, no sign inversion is required.", 1, True),
    ("按负反馈环路约定，需要再取负。", 1, False),
    ("按负反馈环路约定，需要再取负。", -1, True),
    ("The loop must be negated.", 1, False),
    ("The loop must be negated.", -1, True),
    ("Under negative feedback, use the longitudinal open loop.", 1, False),
    ("不确定是否需要再取负。", 1, False),
    ("不确定是否需要再取负。", -1, False),
    ("不是不需要再取负。", 1, False),
    ("不需要再取负；但必须再取负。", 1, False),
    ("不需要再取负；但必须再取负。", -1, False),
    ("Do not negate the loop; the loop must be negated.", 1, False),
    ("Do not negate the loop; the loop must be negated.", -1, False),
    ("不需要再取负；feedback_sign = -1.", -1, False),
    ("不需要再取负；feedback_sign = -1.", 1, False),
])
def test_feedback_sign_requires_consistent_explicit_source(
    local_request, declaration, sign, accepted,
):
    from flight_agent.verification.agent_planner import _validate_local_linearize

    root = Path(local_request.project.root)
    (root / "README.md").write_text(DECLARATIONS.replace(
        "Under negative feedback the loop must not be negated; feedback_sign = 1.",
        declaration,
    ))
    project = discover_matlab_project(str(root))
    inputs = linear_inputs(project)
    inputs.update(feedback_sign=sign, feedback_source=selection_source(declaration))
    typed = contracts.LinearizeInput.model_validate(inputs)
    if accepted:
        _validate_local_linearize(typed, project, {})
    else:
        with pytest.raises(ValueError, match="Feedback sign"):
            _validate_local_linearize(typed, project, {})


def test_feedback_no_negation_quote_still_requires_original_attribution(local_request):
    from flight_agent.verification.agent_planner import _validate_local_linearize

    project = prepare_frequency_project(Path(local_request.project.root))
    inputs = linear_inputs(project)
    inputs["feedback_source"] = selection_source("按负反馈环路约定使用时不需要再取负。")
    with pytest.raises(ValueError, match="original attribution"):
        _validate_local_linearize(contracts.LinearizeInput.model_validate(inputs), project, {})


@pytest.mark.parametrize("document,quote,value,selector,accepted", [
    ("point_index: 7", "feedback_sign = 1", 1, False, False),
    ("point_index: 7", "point_index: 1", 1, False, False),
    ("point_index: 7", "point_index: 1", 1, True, False),
    ("point_index: 7", "feedback_sign = 7", 7, False, False),
    ("Default point_index: 7", "point_index: 1", 1, True, True),
    ("Default point_index: 7", "配平点 1 线性化", 1, True, True),
    ("Default point_index: 7", "point_index: 1", 1, False, False),
    ("Default point_index: 7", "Do not select point_index: 1", 1, True, False),
    ("Default point_index: 7", "不要选择配平点 1", 1, True, False),
    ("Default point_index: 7", "feedback_sign = 1", 1, True, False),
    ("Default point_index: 7", "point_index: 7", 7, False, True),
])
def test_linearization_condition_sources_are_specific_and_respect_authority(
    local_request, document, quote, value, selector, accepted,
):
    from flight_agent.runtime.requirements import CriterionSource
    from flight_agent.verification.agent_planner import _validate_local_linearize

    root = Path(local_request.project.root)
    project = prepare_frequency_project(root)
    (root / "README.md").write_text(DECLARATIONS.replace("point_index: 7", document))
    project = discover_matlab_project(str(root))
    inputs = linear_inputs(project, value)
    source = CriterionSource(kind="user_message", reference="message:request", quote=quote)
    inputs["condition_sources"] = {
        "point_index": {"reference": source.reference, "quote": source.quote}
    }
    if selector:
        entry = next(item for item in project.entries if item.path == "choose_point.m")
        inputs["preparations"] = [{
            "entry": entry.model_copy(update={"arguments": [value]}).model_dump(mode="json"),
            "argument_conditions": ["point_index"],
            "source": selection_source(
                "Preparation: choose_point(pointNumber), before the existing initializer."
            ),
        }]
    typed = contracts.LinearizeInput.model_validate(inputs)
    if accepted:
        _validate_local_linearize(typed, project, {source.reference: source})
    else:
        with pytest.raises(ValueError, match="[Cc]ondition"):
            _validate_local_linearize(typed, project, {source.reference: source})


@pytest.mark.parametrize("declaration,quote,mode,path,accepted", [
    (
        "Do not use the initialized operating point.\n"
        "Use workspace operating point Selected.OperatingPoint.",
        "Do not use the initialized operating point.", "initialized", [], False,
    ),
    (
        "Do not use the initialized operating point.\n"
        "Use workspace operating point Selected.OperatingPoint.",
        "initialized operating point", "initialized", [], False,
    ),
    (
        "Do not use the initialized operating point.\n"
        "Use workspace operating point Selected.OperatingPoint.",
        "Use workspace operating point Selected.OperatingPoint.",
        "workspace", ["Selected", "OperatingPoint"], True,
    ),
    (
        "Use the initialized operating point.\n"
        "Use workspace operating point Selected.OperatingPoint.",
        "Use the initialized operating point.", "initialized", [], False,
    ),
    (
        "Do not use workspace operating point Selected.OperatingPoint.",
        "Do not use workspace operating point Selected.OperatingPoint.",
        "workspace", ["Selected", "OperatingPoint"], False,
    ),
    (
        "The linearization does not use the initialized operating point.",
        "The linearization does not use the initialized operating point.",
        "initialized", [], False,
    ),
    (
        "No initialized operating point is selected.",
        "No initialized operating point is selected.", "initialized", [], False,
    ),
    (
        "避免使用初始化工作点。",
        "避免使用初始化工作点。", "initialized", [], False,
    ),
    (
        "不要使用初始化工作点；使用工作区 Selected.OperatingPoint。",
        "不要使用初始化工作点", "initialized", [], False,
    ),
    (
        "The model has initialized settings.",
        "The model has initialized settings.", "initialized", [], False,
    ),
    (
        "线性化使用当前选定配平点对应的初始化工作点。",
        "线性化使用当前选定配平点对应的初始化工作点。", "initialized", [], True,
    ),
])
def test_operating_point_source_is_affirmative_and_mode_specific(
    local_request, declaration, quote, mode, path, accepted,
):
    from flight_agent.verification.agent_planner import _validate_local_linearize

    root = Path(local_request.project.root)
    (root / "README.md").write_text(DECLARATIONS.replace(
        "Operating point: initialized current selected operating point.", declaration
    ))
    project = discover_matlab_project(str(root))
    inputs = linear_inputs(project)
    inputs["operating_point"] = {
        "mode": mode, "path": path, "source": selection_source(quote)
    }
    typed = contracts.LinearizeInput.model_validate(inputs)
    if accepted:
        _validate_local_linearize(typed, project, {})
    else:
        with pytest.raises(ValueError, match="[Oo]perating point"):
            _validate_local_linearize(typed, project, {})
