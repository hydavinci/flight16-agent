import importlib
import math
from types import SimpleNamespace

import pytest

from flight_agent.runtime.agent_contracts import AgentPlan, AgentScope, validate_bindings
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.runtime.observations import CapabilityOutcome, Observation
from flight_agent.runtime.requirements import Criterion
from tests.agent_helpers import (
    criterion_data,
    observation_data,
    register_rmse,
    verification_plan_data,
)


def adapter():
    return importlib.import_module("flight_agent.verification.adapters.evaluation")


def criterion(**changes):
    return Criterion.model_validate(criterion_data(conditions={}, **changes))


@pytest.mark.parametrize(
    "operator,expected",
    [
        ("<", ["pass", "fail", "fail"]),
        ("<=", ["pass", "pass", "fail"]),
        ("==", ["fail", "pass", "fail"]),
        (">=", ["fail", "pass", "pass"]),
        (">", ["fail", "fail", "pass"]),
    ],
)
def test_all_comparisons_use_exact_unrounded_values_without_hidden_tolerance(operator, expected):
    statuses = []
    for value in [math.nextafter(2.0, -math.inf), 2.0, math.nextafter(2.0, math.inf)]:
        observation = Observation.model_validate(observation_data(value=value, conditions={}))
        result = adapter().evaluate_criteria(
            [criterion(operator=operator)], [observation], overall_requested=True
        )
        statuses.append(result.status)
        assert result.checks[0].value == value
    assert statuses == expected


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"node_id": "different"}, "missing"),
        ({"metric_id": "another_metric"}, "missing"),
        ({"conditions": {"case": "different"}}, "missing"),
        ({"unit": "cm"}, "unit"),
        ({"valid": False, "reason": "Transient window is not stationary."}, "stationary"),
    ],
)
def test_node_metric_conditions_units_and_validity_are_not_interchangeable(changes, reason):
    observation = Observation.model_validate(observation_data(**{"conditions": {}, **changes}))
    result = adapter().evaluate_criteria([criterion()], [observation], overall_requested=True)
    assert result.status == "undetermined"
    assert reason in result.checks[0].reason.lower()


def test_same_metric_in_distinct_conditions_has_independent_criteria():
    criteria = [
        Criterion.model_validate(criterion_data("slow", conditions={"case": "slow"})),
        Criterion.model_validate(criterion_data("fast", conditions={"case": "fast"})),
    ]
    observations = [
        Observation.model_validate(observation_data(value=1, conditions={"case": "slow"})),
        Observation.model_validate(observation_data(value=3, conditions={"case": "fast"})),
    ]
    result = adapter().evaluate_criteria(criteria, observations, overall_requested=True)
    assert result.status == "fail"
    assert [check.status for check in result.checks] == ["pass", "fail"]


def test_duplicate_evidence_does_not_silently_choose_a_favorable_value():
    observations = [
        Observation.model_validate(observation_data(value=value, conditions={})) for value in (1, 3)
    ]
    result = adapter().evaluate_criteria([criterion()], observations, overall_requested=True)
    assert result.status == "undetermined"
    assert "ambiguous" in result.checks[0].reason.lower()


@pytest.mark.parametrize(
    "special,valid,expected",
    [
        ("positive_infinity", True, "fail"),
        ("negative_infinity", True, "pass"),
        ("positive_infinity", False, "undetermined"),
        ("undefined", False, "undetermined"),
    ],
)
def test_structured_special_values_follow_validated_producer_policy(special, valid, expected):
    observation = Observation.model_validate(
        observation_data(
            value=None,
            conditions={},
            special=special,
            valid=valid,
            reason="" if valid else "Producer policy forbids this comparison.",
        )
    )
    result = adapter().evaluate_criteria([criterion()], [observation], overall_requested=True)
    assert result.status == expected
    assert result.checks[0].value is None
    assert result.checks[0].special == special
    assert "Infinity" not in result.model_dump_json()


def test_missing_required_evidence_dominates_known_failure_without_hiding_it():
    criteria = [
        criterion(),
        criterion(id="other-limit", metric={"node_id": "other", "metric_id": "x"}),
    ]
    result = adapter().evaluate_criteria(
        criteria,
        [Observation.model_validate(observation_data(value=3, conditions={}))],
        overall_requested=True,
    )
    assert result.status == "undetermined"
    assert [check.status for check in result.checks] == ["fail", "undetermined"]


def test_interval_boundaries_do_not_force_an_unrequested_overall_decision():
    criteria = [
        criterion(id="lower", operator=">=", threshold=1),
        criterion(id="upper", threshold=2),
    ]
    result = adapter().evaluate_criteria(
        criteria,
        [Observation.model_validate(observation_data(value=1.5, conditions={}))],
        overall_requested=False,
    )
    assert result.status is None
    assert [check.status for check in result.checks] == ["pass", "pass"]


def test_analysis_does_not_create_an_empty_automatic_evaluation():
    with pytest.raises(ValueError, match="criteria"):
        adapter().evaluate_criteria([], [], overall_requested=False)


def evaluation_tools():
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])
    registry.register(adapter().scalar_evaluation_capability())
    return registry


def evaluation_plan():
    data = verification_plan_data()
    data["requirements"]["criteria"][0]["conditions"] = {}
    data["nodes"][1]["inputs"] = {
        "criteria": data["requirements"]["criteria"],
        "overall_requested": True,
    }
    return data


@pytest.mark.parametrize(
    "mutation", ["criteria", "overall", "literal-evidence", "missing-producer"]
)
def test_evaluation_preflight_cannot_replace_criteria_or_forge_evidence(mutation):
    data = evaluation_plan()
    if mutation == "criteria":
        data["nodes"][1]["inputs"]["criteria"] = [
            {**data["requirements"]["criteria"][0], "threshold": 100}
        ]
    elif mutation == "overall":
        data["nodes"][1]["inputs"]["overall_requested"] = False
    elif mutation == "literal-evidence":
        del data["nodes"][1]["bindings"]
        data["nodes"][1]["inputs"]["observations"] = [observation_data()]
    else:
        del data["nodes"][1]["bindings"]
        data["nodes"][1]["depends_on"] = []
    with pytest.raises(ValueError):
        validate_bindings(AgentPlan.model_validate(data), evaluation_tools())


def test_registered_evaluator_uses_real_direct_dependency_outputs():
    tools = evaluation_tools()
    plan = AgentPlan.model_validate(evaluation_plan())
    validate_bindings(plan, tools)
    observed = Observation.model_validate(observation_data(value=3, conditions={}))
    context = SimpleNamespace(outputs={"compute": CapabilityOutcome(observations=[observed])})
    result = tools.invoke(
        "evaluate_criteria",
        {**plan.nodes[1].inputs, "observations": [observed.model_dump(mode="json")]},
        context,
        AgentScope(baseline_version=1, confirmed=True),
    )
    assert result["evaluation"]["status"] == "fail"
    with pytest.raises(ValueError, match="evidence|depend"):
        tools.invoke(
            "evaluate_criteria",
            {**plan.nodes[1].inputs, "observations": [observation_data(value=0, conditions={})]},
            context,
            AgentScope(baseline_version=1, confirmed=True),
        )


def test_registered_evaluator_combines_multiple_producers_without_an_expression_binding():
    tools = evaluation_tools()
    data = evaluation_plan()
    data["nodes"].insert(1, {**data["nodes"][0], "id": "other"})
    data["nodes"][2]["depends_on"].append("other")
    data["nodes"][2]["bindings"] = {}
    data["requirements"]["criteria"].append(
        criterion_data(
            "other-limit", conditions={}, metric={"node_id": "other", "metric_id": "rmse"}
        )
    )
    data["nodes"][2]["inputs"]["criteria"] = data["requirements"]["criteria"]
    plan = AgentPlan.model_validate(data)
    validate_bindings(plan, tools)
    ctx = SimpleNamespace(
        outputs={
            node_id: CapabilityOutcome(
                observations=[
                    Observation.model_validate(
                        observation_data(node_id=node_id, value=value, conditions={})
                    )
                ]
            )
            for node_id, value in [("compute", 1), ("other", 3)]
        }
    )
    result = tools.invoke(
        "evaluate_criteria",
        plan.nodes[2].inputs,
        ctx,
        AgentScope(baseline_version=1, confirmed=True),
    )
    assert [check["status"] for check in result["evaluation"]["checks"]] == ["pass", "fail"]
    assert result["evaluation"]["status"] == "fail"
