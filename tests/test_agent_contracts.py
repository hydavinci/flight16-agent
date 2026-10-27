import importlib

import pytest
from pydantic import ValidationError

from flight_agent.runtime.contracts import fingerprint
from tests.agent_helpers import (
    analysis_plan_data,
    check_data,
    criterion_data,
    gap_data,
    invocation_spec_data,
    observation_data,
    resource_data,
    verification_plan_data,
)


def contract(module, name):
    try:
        return getattr(importlib.import_module(f"flight_agent.runtime.{module}"), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"Task 1 contract is missing: {module}.{name}: {exc}")


def plan(data=None):
    return contract("agent_contracts", "AgentPlan").model_validate(
        analysis_plan_data() if data is None else data
    )


def test_analysis_requires_neither_design_pair_nor_criteria():
    result = plan()
    assert result.requirements.criteria == []
    assert result.evaluation_node is None
    assert not hasattr(result, "design")


def test_draft_rejects_round_budget_below_node_count_including_delivery():
    from tests.agent_helpers import delivery_draft

    data = delivery_draft()
    data["nodes"].insert(1, {**data["nodes"][0], "id": "second-analysis"})
    data["nodes"][-1]["depends_on"].append("second-analysis")
    data["budget"].update(max_rounds=2, max_calls=3)
    model = contract("agent_contracts", "AgentPlanDraft")
    with pytest.raises(ValidationError, match="round budget"):
        model.model_validate(data)
    data["budget"]["max_rounds"] = 3
    assert len(model.model_validate(data).nodes) == 3


def test_verification_without_criteria_is_rejected():
    data = analysis_plan_data()
    data["requirements"]["mode"] = "verification"
    with pytest.raises(ValidationError, match="criteria"):
        plan(data)


@pytest.mark.parametrize("field", ["criteria", "overall_requested", "evaluation_node"])
def test_analysis_rejects_compliance_requests(field):
    data = analysis_plan_data()
    if field == "criteria":
        data["requirements"][field] = [criterion_data()]
    elif field == "overall_requested":
        data["requirements"][field] = True
    else:
        data[field] = "compute"
    with pytest.raises(ValidationError, match="analysis|Analysis"):
        plan(data)


def test_verification_accepts_distinct_criteria_for_the_same_metric_in_different_conditions():
    data = verification_plan_data()
    data["requirements"]["criteria"].append(
        criterion_data("second-case", conditions={"case": "second"}, threshold=3.0)
    )
    assert [item.id for item in plan(data).requirements.criteria] == ["rmse-limit", "second-case"]


@pytest.mark.parametrize(
    "change",
    ["node", "criterion", "output", "resource", "gap", "dependency", "effect"],
)
def test_duplicate_ids_and_permissions_are_rejected(change):
    data = verification_plan_data()
    if change in ("node", "dependency"):
        if change == "node":
            data["nodes"].append(data["nodes"][0])
        else:
            data["nodes"][1]["depends_on"] = ["compute", "compute"]
    elif change in ("criterion", "output"):
        field = "criteria" if change == "criterion" else "requested_outputs"
        data["requirements"][field].append(data["requirements"][field][0])
    elif change == "resource":
        data["resources"] = [resource_data(), resource_data()]
    elif change == "gap":
        data["gaps"] = [gap_data(), gap_data()]
    else:
        data["allowed_effects"] = ["read_only", "read_only"]
    with pytest.raises(ValidationError, match="unique|duplicate"):
        plan(data)


@pytest.mark.parametrize(
    "change",
    [
        "cycle",
        "dependency",
        "binding",
        "nonancestor",
        "self",
        "resource",
        "output",
        "criterion",
        "evaluation",
        "no-evaluation",
        "gap",
        "input-collision",
    ],
)
def test_plans_reject_dangling_or_illegal_references(change):
    data = verification_plan_data()
    if change == "cycle":
        data["nodes"][0]["depends_on"] = ["evaluate"]
    elif change == "dependency":
        data["nodes"][1]["depends_on"] = ["missing"]
    elif change in ("binding", "nonancestor", "self"):
        data["nodes"][1]["bindings"]["observations"]["node_id"] = (
            "missing" if change == "binding" else "evaluate" if change == "self" else "compute"
        )
        if change == "nonancestor":
            data["nodes"][1]["depends_on"] = []
    elif change == "resource":
        data["nodes"][0]["resource_bindings"] = {"data": "missing"}
    elif change == "output":
        data["requirements"]["requested_outputs"][0]["node_id"] = "missing"
    elif change == "criterion":
        data["requirements"]["criteria"][0]["metric"]["node_id"] = "missing"
    elif change in ("evaluation", "no-evaluation"):
        data["evaluation_node"] = "missing" if change == "evaluation" else None
    elif change == "gap":
        data["gaps"] = [gap_data(requirement_id="missing")]
    else:
        data["nodes"][1]["inputs"] = {"observations": []}
    with pytest.raises(ValidationError):
        plan(data)


def test_resource_binding_is_server_side_while_draft_contains_only_ids():
    data = analysis_plan_data()
    data["resources"] = [resource_data()]
    data["nodes"][0]["resource_bindings"] = {"data": "measurements"}
    assert plan(data).resources[0].location == "uploads"
    draft_data = {key: value for key, value in data.items() if key != "resources"}
    draft_data["resource_ids"] = ["measurements"]
    draft = contract("agent_contracts", "AgentPlanDraft").model_validate(draft_data)
    assert "locator" not in draft.model_dump_json()
    assert "resources" not in draft.model_dump()
    with pytest.raises(ValidationError):
        contract("agent_contracts", "AgentPlanDraft").model_validate(data)
    with pytest.raises(ValidationError):
        plan(draft_data)


@pytest.mark.parametrize("field", ["node_id", "item_id"])
def test_unresolved_output_requires_an_explicit_gap(field):
    data = analysis_plan_data()
    data["requirements"]["requested_outputs"][0][field] = None
    with pytest.raises(ValidationError, match="gap|producer"):
        plan(data)
    data["gaps"] = [gap_data()]
    result = plan(data)
    assert result.gaps[0].requirement_id == "rmse-output"
    assert result.gaps[0].limitation


def test_partial_plan_can_have_no_producer_but_cannot_claim_an_unknown_producer():
    data = analysis_plan_data()
    data["nodes"] = []
    data["requirements"]["requested_outputs"][0].update(node_id=None, item_id=None)
    data["gaps"] = [gap_data()]
    assert plan(data).nodes == []
    data["requirements"]["requested_outputs"][0]["node_id"] = "missing"
    with pytest.raises(ValidationError):
        plan(data)


@pytest.mark.parametrize(
    "change", ["criterion-source", "file-digest", "example-digest", "operator"]
)
def test_criteria_require_traceable_sources_and_comparison_operators(change):
    data = criterion_data()
    if change == "criterion-source":
        del data["source"]
    elif change in ("file-digest", "example-digest"):
        data["source"]["kind"] = "standard_file" if change == "file-digest" else "accepted_example"
    else:
        data["operator"] = "x <= threshold"
    with pytest.raises(ValidationError):
        contract("requirements", "Criterion").model_validate(data)


def test_file_source_preserves_provided_digest_without_inventing_a_version():
    source = contract("requirements", "CriterionSource").model_validate(
        {
            "kind": "standard_file",
            "reference": "standard-1",
            "quote": "RMSE at most 2 m.",
            "sha256": "a" * 64,
        }
    )
    assert source.sha256 == "a" * 64
    assert source.version is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize(
    "target", ["input", "threshold", "resource", "conditions", "table", "details"]
)
def test_all_numeric_boundaries_reject_nonfinite_json(target, value):
    if target == "input":
        data = analysis_plan_data()
        data["nodes"][0]["inputs"]["observed"] = [value]
        model, payload = contract("agent_contracts", "AgentPlan"), data
    elif target == "threshold":
        model, payload = contract("requirements", "Criterion"), criterion_data(threshold=value)
    elif target == "resource":
        model, payload = (
            contract("resources", "ResourceReference"),
            resource_data(metadata={"nested": [value]}),
        )
    elif target == "conditions":
        model, payload = (
            contract("observations", "Observation"),
            observation_data(conditions={"nested": [value]}),
        )
    elif target == "table":
        model, payload = (
            contract("observations", "ResultTable"),
            {
                "id": "table",
                "columns": ["value"],
                "rows": [[value]],
                "evidence": [],
            },
        )
    else:
        model, payload = (
            contract("observations", "CapabilityOutcome"),
            {
                "details": {"nested": [value]},
            },
        )
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "module,name,payload",
    [
        ("agent_contracts", "AgentPlan", analysis_plan_data()),
        ("agent_contracts", "AgentNode", analysis_plan_data()["nodes"][0]),
        ("agent_contracts", "InvocationSpec", invocation_spec_data()),
        ("requirements", "Criterion", criterion_data()),
        ("resources", "ResourceReference", resource_data()),
        ("observations", "Observation", observation_data()),
        ("observations", "CapabilityOutcome", {}),
        ("observations", "TaskResult", {"execution_status": "completed"}),
    ],
)
def test_new_contracts_reject_extra_fields(module, name, payload):
    with pytest.raises(ValidationError, match="Extra inputs"):
        contract(module, name).model_validate({**payload, "surprise": True})


@pytest.mark.parametrize("path", [[], ["x"] * 17, [True], [-1], [1.5], [{}], ["x" * 201]])
def test_output_reference_paths_are_nonempty_bounded_strings_or_nonnegative_integers(path):
    with pytest.raises(ValidationError):
        contract("agent_contracts", "OutputReference").model_validate(
            {"node_id": "compute", "path": path}
        )


def test_output_reference_accepts_nested_json_selection():
    value = contract("agent_contracts", "OutputReference").model_validate(
        {"node_id": "compute", "path": ["observations", 0, "value"]}
    )
    assert value.path == ["observations", 0, "value"]


@pytest.mark.parametrize("change", ["title", "nodes", "text", "json-list", "budget", "name"])
def test_contract_size_and_budget_limits_are_enforced(change):
    data = analysis_plan_data()
    if change == "title":
        data["title"] = "x" * 201
    elif change == "nodes":
        data["nodes"] = [{**data["nodes"][0], "id": f"node-{index}"} for index in range(41)]
    elif change == "text":
        data["requirements"]["objective"] = "x" * 6001
    elif change == "json-list":
        data["nodes"][0]["inputs"] = {"values": [0] * 10001}
    elif change == "budget":
        data["budget"]["max_calls"] = 1
        data["nodes"].append({"id": "other", "tool": "measure", "tool_version": "1"})
    else:
        data["requirements"]["requested_outputs"][0]["item_id"] = "invalid.expression"
    with pytest.raises(ValidationError):
        plan(data)


def test_metric_value_reexport_preserves_legacy_json_and_special_value_semantics():
    from flight_agent.verification.contracts import MetricValue as LegacyMetricValue

    metric = contract("observations", "MetricValue")
    assert LegacyMetricValue is metric
    assert metric(value=1.0, unit="m").model_dump() == {
        "value": 1.0,
        "unit": "m",
        "special": None,
        "valid": True,
        "reason": "",
    }
    infinite = metric(unit="dB", special="positive_infinity")
    assert '"value":null' in infinite.model_dump_json()
    assert "Infinity" not in infinite.model_dump_json()
    for invalid in (
        {"unit": "m"},
        {"unit": "m", "value": 1.0, "special": "positive_infinity"},
        {"unit": "m", "valid": False},
        {"unit": "m", "special": "undefined"},
    ):
        with pytest.raises(ValidationError):
            metric.model_validate(invalid)


@pytest.mark.parametrize(
    "change",
    [{"value": None}, {"valid": False}, {"special": "positive_infinity"}, {"value": float("nan")}],
)
def test_observation_reuses_metric_validity_constraints(change):
    with pytest.raises(ValidationError):
        contract("observations", "Observation").model_validate(observation_data(**change))


def test_invalid_observation_has_explicit_reason_without_a_compliance_decision():
    result = contract("observations", "CapabilityOutcome").model_validate(
        {
            "observations": [observation_data(value=None, valid=False, reason="No samples.")],
        }
    )
    assert not result.observations[0].valid
    assert result.evaluation is None


@pytest.mark.parametrize(
    "change",
    [{"value_unit": "cm"}, {"value": None}, {"special": "undefined", "value": None}],
)
def test_decisive_checks_require_a_usable_value_in_the_exact_criterion_unit(change):
    with pytest.raises(ValidationError):
        contract("observations", "CriterionCheck").model_validate(check_data(**change))
    result = contract("observations", "CriterionCheck").model_validate(
        check_data(status="undetermined", reason="Cannot compare this value.", **change)
    )
    assert result.status == "undetermined"


@pytest.mark.parametrize("change", ["empty", "duplicate", "incomplete", "wrong-status"])
def test_evaluation_cannot_claim_a_decision_from_missing_or_inconsistent_checks(change):
    data = {"status": "pass", "checks": [check_data()]}
    if change == "empty":
        data["checks"] = []
    elif change == "duplicate":
        data["checks"].append(check_data())
    elif change == "incomplete":
        data["checks"][0].update(status="undetermined", reason="Missing evidence.")
    else:
        data["status"] = "fail"
    with pytest.raises(ValidationError):
        contract("observations", "EvaluationResult").model_validate(data)


def test_tables_require_unique_columns_and_rectangular_finite_rows():
    table = contract("observations", "ResultTable")
    assert table(id="summary", columns=["name", "value"], rows=[["RMSE", 1.0]]).rows == [
        ["RMSE", 1.0]
    ]
    for columns, rows in [(["value", "value"], [[1, 2]]), (["value"], [[1, 2]])]:
        with pytest.raises(ValidationError):
            table(id="summary", columns=columns, rows=rows)


@pytest.mark.parametrize("effect", ["write_candidate", "adopt_candidate"])
def test_candidate_permissions_are_rejected_by_plan_and_scope_even_if_constructed(effect):
    data = analysis_plan_data()
    data["allowed_effects"] = [effect]
    with pytest.raises(ValidationError):
        plan(data)
    scope = contract("agent_contracts", "AgentScope")
    with pytest.raises(ValidationError):
        scope(baseline_version=1, confirmed=True, resources=[], allowed_effects=[effect])
    bypass = scope.model_construct(
        baseline_version=1,
        confirmed=True,
        resources=[],
        allowed_effects=(effect,),
    )
    with pytest.raises(ValueError):
        bypass.authorize(effect)


def test_scope_requires_confirmation_and_exact_permission():
    scope = contract("agent_contracts", "AgentScope")
    allowed = scope(baseline_version=1, confirmed=True, resources=[], allowed_effects=["read_only"])
    assert allowed.authorize("read_only") is None
    with pytest.raises(ValueError, match="permission"):
        allowed.authorize("execute_project")
    unconfirmed = scope(baseline_version=1, confirmed=False, resources=[])
    with pytest.raises(ValueError, match="confirmed"):
        unconfirmed.authorize("read_only")


def test_baseline_adds_confirmation_without_design_or_resource_ids():
    baseline = contract("agent_contracts", "AgentBaseline").model_validate(
        {
            **analysis_plan_data(),
            "version": 1,
            "confirmed": True,
            "confirmed_at": "2026-10-21T12:00:00Z",
        }
    )
    assert baseline.version == 1
    assert baseline.confirmed
    with pytest.raises(ValidationError):
        contract("agent_contracts", "AgentBaseline").model_validate(
            {
                **analysis_plan_data(),
                "version": 0,
            }
        )
    with pytest.raises(ValidationError):
        contract("agent_contracts", "AgentBaseline").model_validate(
            {
                **analysis_plan_data(),
                "version": 1,
                "confirmed": True,
            }
        )


@pytest.mark.parametrize("change", list(invocation_spec_data()))
def test_invocation_identity_covers_every_actual_dispatch_field(change):
    spec_type = contract("agent_contracts", "InvocationSpec")
    data = invocation_spec_data()
    original = spec_type.model_validate(data)
    reordered = {key: data[key] for key in reversed(data)}
    assert spec_type.model_validate(reordered).fingerprint == original.fingerprint
    if change in ("capability", "capability_version"):
        data[change] = "other" if change == "capability" else "2"
    elif change in ("resource_fingerprints", "dependency_output_sha256"):
        data[change] = {"changed": "c" * 64}
    else:
        data[change] = {"changed": 2}
    assert spec_type.model_validate(data).fingerprint != original.fingerprint


def test_invocation_run_revalidates_stable_v2_output_and_digest():
    outcome = contract("observations", "CapabilityOutcome")(observations=[observation_data()])
    data = {
        "id": "1" * 32,
        "spec": invocation_spec_data(),
        "output": outcome,
        "output_sha256": fingerprint(outcome.model_dump(mode="json")),
        "started_at": "2026-10-21T12:00:00Z",
        "finished_at": "2026-10-21T12:00:01Z",
    }
    run_type = contract("agent_contracts", "InvocationRun")
    run = run_type.model_validate(data)
    assert run.schema_version == "2.0"
    assert run.output.observations[0].value == 1.0
    for changes in (
        {"output_sha256": "f" * 64},
        {"schema_version": "1.0"},
        {"output": {"value": 1.0}},
    ):
        with pytest.raises(ValidationError):
            run_type.model_validate({**data, **changes})
    run.output.observations.append(
        contract("observations", "Observation").model_validate(observation_data(metric_id="other"))
    )
    with pytest.raises(ValidationError, match="digest"):
        run_type.model_validate(run)


@pytest.mark.parametrize("target", ["plan", "node", "observation", "spec", "scope"])
def test_model_construct_cannot_bypass_consuming_boundaries(target):
    if target == "plan":
        model = contract("agent_contracts", "AgentPlan")
        value = model.model_construct(**{**analysis_plan_data(), "nodes": []})
    elif target == "node":
        data = analysis_plan_data()
        data["nodes"] = [
            contract("agent_contracts", "AgentNode").model_construct(
                id="compute",
                tool="offline_rmse",
                tool_version="1",
                inputs={"value": float("inf")},
            )
        ]
        with pytest.raises(ValidationError):
            plan(data)
        return
    elif target == "observation":
        value = contract("observations", "Observation").model_construct(
            **observation_data(valid=False)
        )
        with pytest.raises(ValidationError):
            contract("observations", "CapabilityOutcome")(observations=[value])
        return
    elif target == "spec":
        value = contract("agent_contracts", "InvocationSpec").model_construct(
            **{**invocation_spec_data(), "inputs": {"value": float("inf")}}
        )
        with pytest.raises(ValueError):
            _ = value.fingerprint
        return
    else:
        value = contract("agent_contracts", "AgentScope").model_construct(
            baseline_version=0,
            confirmed=True,
            resources=[],
            allowed_effects=("read_only",),
        )
        with pytest.raises(ValueError):
            value.authorize("read_only")
        return
    with pytest.raises(ValidationError):
        model.model_validate(value)


def test_completed_result_cannot_contain_unmet_requirements():
    with pytest.raises(ValidationError, match="partial|unmet"):
        contract("observations", "TaskResult")(
            execution_status="completed",
            unmet_requirements=[gap_data()],
        )
    partial = contract("observations", "TaskResult")(
        execution_status="partial",
        unmet_requirements=[gap_data()],
    )
    assert partial.unmet_requirements[0].requirement_id == "rmse-output"


@pytest.mark.parametrize(
    "change",
    [
        "missing-evaluation",
        "missing-check",
        "wrong-criterion",
        "missing-output",
        "wrong-metric",
        "wrong-threshold",
        "wrong-unit",
        "analysis-evaluation",
    ],
)
def test_completed_result_is_checked_against_actual_requirements(change):
    data = verification_plan_data()
    result_data = {
        "execution_status": "completed",
        "observations": [observation_data()],
        "evaluation": {"status": "pass", "checks": [check_data()]},
    }
    if change == "missing-evaluation":
        result_data["evaluation"] = None
    elif change == "missing-check":
        data["requirements"]["criteria"].append(criterion_data("second-case"))
    elif change == "wrong-criterion":
        result_data["evaluation"]["checks"][0]["criterion_id"] = "other"
    elif change == "missing-output":
        result_data["observations"] = []
    elif change == "wrong-metric":
        result_data["evaluation"]["checks"][0]["metric"]["metric_id"] = "other"
    elif change == "wrong-threshold":
        result_data["evaluation"]["checks"][0]["threshold"] = 3.0
    elif change == "wrong-unit":
        result_data["evaluation"]["checks"][0].update(unit="cm", value_unit="cm")
    else:
        data = analysis_plan_data()
    result = contract("observations", "TaskResult").model_validate(result_data)
    with pytest.raises(ValueError):
        result.validate_requirements(plan(data).requirements)


def test_result_execution_success_is_distinct_from_numeric_validity_and_acceptance():
    requirements = plan(verification_plan_data()).requirements
    result = contract("observations", "TaskResult").model_validate(
        {
            "execution_status": "completed",
            "observations": [observation_data(value=None, valid=False, reason="No samples.")],
            "evaluation": {
                "status": "undetermined",
                "checks": [
                    check_data(
                        value=None,
                        status="undetermined",
                        reason="No samples.",
                    )
                ],
            },
        }
    )
    assert result.validate_requirements(requirements) is None
    assert result.execution_status == "completed"
    assert not result.observations[0].valid
    assert result.evaluation.status == "undetermined"


def test_partial_result_must_disclose_each_missing_output():
    requirements = plan().requirements
    result_type = contract("observations", "TaskResult")
    with pytest.raises(ValueError, match="gap|unmet|missing"):
        result_type(execution_status="partial").validate_requirements(requirements)
    assert (
        result_type(
            execution_status="partial",
            unmet_requirements=[gap_data()],
        ).validate_requirements(requirements)
        is None
    )


def test_budget_state_is_persistable_early_without_a_counter_engine():
    budget_type = contract("agent_contracts", "BudgetState")
    state = budget_type(started_at="2026-10-21T12:00:00Z", calls=2, rounds=1, no_progress=0)
    assert budget_type.model_validate_json(state.model_dump_json()) == state
    for field in ("calls", "rounds", "no_progress"):
        with pytest.raises(ValidationError):
            budget_type.model_validate({**state.model_dump(), field: -1})


@pytest.mark.parametrize("kind", ["finding", "table"])
def test_result_matches_each_nonmetric_output_to_its_producer(kind):
    data = analysis_plan_data()
    data["nodes"].append({**data["nodes"][0], "id": "second"})
    data["requirements"]["requested_outputs"] = [
        {
            "id": producer,
            "kind": kind,
            "node_id": producer,
            "item_id": "summary",
            "description": "A separate result from each producer.",
        }
        for producer in ("compute", "second")
    ]
    item = {"id": "summary", "node_id": "compute"}
    item.update({"text": "Result."} if kind == "finding" else {"columns": ["x"], "rows": []})
    result_type = contract("observations", "TaskResult")
    result = result_type(execution_status="completed", **{f"{kind}s": [item]})
    with pytest.raises(ValueError, match="second"):
        result.validate_requirements(plan(data).requirements)


def test_artifact_requirements_match_logical_identity_not_extension():
    data = analysis_plan_data()
    data["requirements"]["requested_outputs"] = [
        {
            "id": "report-output",
            "kind": "artifact",
            "node_id": "compute",
            "item_id": "report",
            "description": "The requested report.",
        }
    ]
    artifact = {
        "id": "unrelated",
        "node_id": "compute",
        "path": "report.docx",
        "sha256": "a" * 64,
        "size": 12,
    }
    result_type = contract("observations", "TaskResult")
    result = result_type(execution_status="completed", artifacts=[artifact])
    with pytest.raises(ValueError, match="report-output"):
        result.validate_requirements(plan(data).requirements)
    artifact["id"] = "report"
    result_type(execution_status="completed", artifacts=[artifact]).validate_requirements(
        plan(data).requirements
    )


def test_constructed_legacy_budget_is_revalidated_at_v2_boundary():
    from flight_agent.runtime.contracts import ResourceBudget

    data = analysis_plan_data()
    data["budget"] = ResourceBudget.model_construct(max_calls=1000000, max_seconds=-1)
    with pytest.raises(ValidationError):
        plan(data)


def test_constructed_legacy_artifact_is_revalidated_at_v2_boundary():
    from flight_agent.runtime.contracts import EvidenceFile

    artifact = EvidenceFile.model_construct(path="../outside", sha256="bad", size=-1)
    with pytest.raises(ValidationError):
        contract("observations", "CapabilityOutcome")(artifacts=[artifact])
    outcome = contract("observations", "CapabilityOutcome")()
    with pytest.raises(ValidationError):
        contract("agent_contracts", "InvocationRun")(
            id="1" * 32,
            spec=invocation_spec_data(),
            output=outcome,
            output_sha256=fingerprint(outcome.model_dump(mode="json")),
            started_at="start",
            finished_at="finish",
            artifacts=[artifact],
        )


@pytest.mark.parametrize("large", [{"x": "a" * 1000000}, {"a" * 1000000: "x"}])
def test_json_strings_and_keys_are_bounded(large):
    with pytest.raises(ValidationError, match="limit"):
        contract("observations", "CapabilityOutcome")(details=large)


def test_json_has_a_cumulative_size_limit():
    with pytest.raises(ValidationError, match="limit"):
        contract("observations", "CapabilityOutcome")(
            details={str(i): "a" * 5000 for i in range(1000)},
        )


@pytest.mark.parametrize("status", ["partial", "failed", "interrupted"])
def test_unfinished_verification_records_no_fabricated_evaluation(status):
    result = contract("observations", "TaskResult")(
        execution_status=status,
        unmet_requirements=[gap_data()],
    )
    assert result.validate_requirements(plan(verification_plan_data()).requirements) is None
