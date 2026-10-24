import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Annotated

import pytest
from pydantic import Field

from flight_agent.models import FiniteNumber
from flight_agent.runtime.agent_contracts import (
    AgentBaseline,
    AgentPlan,
    AgentScope,
    OutputReference,
    resolve_inputs,
    validate_bindings,
)
from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import (
    Availability,
    CapabilityMetadata,
    MetricDefinition,
    ResourceRequirement,
)
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.loop import LoopDecision, run_loop
from flight_agent.runtime.observations import CapabilityOutcome, Observation, ResultTable
from flight_agent.runtime.resources import AgentContract, ResourceReference
from tests.agent_helpers import (
    analysis_plan_data,
    register_rmse,
    resource_data,
    verification_plan_data,
)


class ObservationInput(AgentContract):
    observations: Annotated[list[Observation], Field(max_length=200)]


class ScalarInput(AgentContract):
    value: FiniteNumber


def scope(**changes):
    return AgentScope(baseline_version=1, confirmed=True, **changes)


def context():
    return SimpleNamespace(node_id="compute", call_id="1" * 32)


def sink(**changes):
    return Capability(
        **{
            "name": "consume",
            "version": "1",
            "description": "Consume declared observations.",
            "input_model": ObservationInput,
            "output_model": CapabilityOutcome,
            "effect": "read_only",
            "operation": lambda context, inputs: CapabilityOutcome(),
            "metadata": CapabilityMetadata(validity_policy={"version": "1"}),
            **changes,
        }
    )


def tools():
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])
    registry.register(sink())
    return registry


def bound_plan(path=None, input_name="observations", **changes):
    data = analysis_plan_data()
    data["nodes"].append(
        {
            "id": "consume",
            "tool": "consume",
            "tool_version": "1",
            "depends_on": ["compute"],
            "bindings": {input_name: {"node_id": "compute", "path": path or ["observations"]}},
            **changes,
        }
    )
    return AgentPlan.model_validate(data)


def outcome():
    return CapabilityOutcome(
        observations=[
            Observation(node_id="compute", metric_id="rmse", label="RMSE", value=2.0, unit="m")
        ]
    )


def test_registered_plugin_computes_real_rmse_in_independent_catalogs():
    registry = CapabilityRegistry(agent_mode=True)
    calls = []
    register_rmse(registry, calls, unit="arbitrary-new-unit")
    result = registry.invoke(
        "offline_rmse", {"observed": [0, 2], "reference": [0, 0]}, context(), scope()
    )
    assert result["observations"][0]["value"] == pytest.approx(2**0.5)
    assert result["observations"][0]["unit"] == "arbitrary-new-unit"
    assert calls == ["1" * 32]
    with pytest.raises(ValueError, match="registered"):
        register_rmse(registry, calls)
    assert CapabilityRegistry(agent_mode=True).catalog() == []


@pytest.mark.parametrize(
    "inputs",
    [
        {"observed": [], "reference": []},
        {"observed": [1], "reference": [1, 2]},
        {"observed": [float("nan")], "reference": [0]},
        {"observed": [1] * 129, "reference": [0] * 129},
        {"observed": [1], "reference": [0], "code": "execute"},
    ],
)
def test_invalid_plugin_inputs_are_rejected_before_operation(inputs):
    registry = CapabilityRegistry(agent_mode=True)
    calls = []
    register_rmse(registry, calls)
    with pytest.raises(ValueError):
        registry.invoke("offline_rmse", inputs, context(), scope())
    assert calls == []


@pytest.mark.parametrize("change", [{"metadata": None}, {"output_model": ScalarInput}])
def test_agent_registry_rejects_missing_declarations_and_nonstable_envelopes(change):
    with pytest.raises(ValueError, match="metadata|CapabilityOutcome"):
        CapabilityRegistry(agent_mode=True).register(replace(sink(), **change))


def test_declarations_reject_duplicates_and_nonjson_execution_code():
    with pytest.raises(ValueError, match="unique"):
        CapabilityMetadata(
            metrics=[
                MetricDefinition(id="rmse", label="RMSE", unit="m"),
                MetricDefinition(id="rmse", label="Duplicate", unit="m"),
            ]
        )
    with pytest.raises(ValueError, match="unique"):
        CapabilityMetadata(
            resources=[
                ResourceRequirement(role="data", kind="signals"),
                ResourceRequirement(role="data", kind="signals"),
            ]
        )
    with pytest.raises(ValueError):
        CapabilityMetadata(validity_policy={"execute": lambda: 1})


@pytest.mark.parametrize("mutation", ["unknown_metric", "unit", "extra", "special"])
def test_outputs_cannot_forge_declared_metrics_or_bypass_stable_schema(mutation):
    value = outcome().model_dump(mode="json")
    if mutation == "extra":
        value["code"] = "execute"
    else:
        observation = value["observations"][0]
        if mutation == "unknown_metric":
            observation["metric_id"] = "invented"
        elif mutation == "unit":
            observation["unit"] = "s"
        else:
            observation.update(value=None, special="positive_infinity")
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [], operation=lambda context, inputs: value)
    with pytest.raises(ValueError):
        registry.invoke("offline_rmse", {"observed": [1], "reference": [0]}, context(), scope())


def test_special_values_require_explicit_comparability_or_invalid_reason():
    registry = CapabilityRegistry(agent_mode=True)
    registry.register(
        sink(
            metadata=CapabilityMetadata(
                metrics=[
                    MetricDefinition(
                        id="rmse",
                        label="RMSE",
                        unit="m",
                        comparable_specials=["positive_infinity"],
                    )
                ]
            )
        )
    )
    special = outcome().model_dump(mode="json")
    special["observations"][0].update(value=None, special="positive_infinity")
    assert registry.validate_result("consume", special)["observations"][0]["valid"] is True
    special["observations"][0].update(
        special="negative_infinity", valid=False, reason="Not comparable under this policy."
    )
    assert registry.validate_result("consume", special)["observations"][0]["valid"] is False


def test_readiness_is_serialized_with_reason_but_never_its_callable():
    registry = CapabilityRegistry(agent_mode=True)

    def readiness(resources):
        return Availability(
            state="available" if resources else "needs_input", reason="Supply signal data."
        )

    registry.register(sink(readiness=readiness))
    catalog = registry.catalog()
    assert catalog[0]["availability"] == {"state": "needs_input", "reason": "Supply signal data."}
    assert "function" not in json.dumps(catalog)
    supplied = (ResourceReference.model_validate(resource_data()),)
    assert registry.availability("consume", supplied).state == "available"
    with pytest.raises(ValueError, match="reason"):
        Availability(state="unavailable", reason="")


def test_invocation_checks_resource_readiness_without_computation():
    registry = CapabilityRegistry(agent_mode=True)
    called = []
    registry.register(
        sink(
            operation=lambda context, inputs: called.append("executed"),
            metadata=CapabilityMetadata(
                resources=[ResourceRequirement(role="data", kind="signal_data", location="uploads")]
            ),
        )
    )
    with pytest.raises(ValueError, match="resource|data"):
        registry.invoke("consume", {"observations": []}, context(), scope())
    assert called == []


@pytest.mark.parametrize("effect", ["execute_project", "write_delivery"])
def test_effects_outside_confirmed_agent_scope_are_blocked(effect):
    registry = CapabilityRegistry(agent_mode=True)
    registry.register(sink(effect=effect))
    with pytest.raises(ValueError, match="permission"):
        registry.invoke("consume", {"observations": []}, context(), scope())


@pytest.mark.parametrize("effect", ["write_candidate", "adopt_candidate"])
def test_agent_registry_rejects_candidate_effects(effect):
    with pytest.raises(ValueError, match="[Cc]andidate"):
        CapabilityRegistry(agent_mode=True).register(sink(effect=effect))


def test_forged_scope_cannot_skip_confirmation_or_effect_validation():
    registry = tools()
    forged = AgentScope.model_construct(baseline_version=1, confirmed="yes")
    with pytest.raises(ValueError):
        registry.invoke("consume", {"observations": []}, context(), forged)
    with pytest.raises(ValueError):
        registry.invoke(
            "consume",
            {"observations": []},
            context(),
            AgentScope(baseline_version=1, confirmed=False),
        )


def test_common_loop_accepts_agent_scope_and_persists_real_output():
    values = []
    saved = []
    decision = run_loop(
        decide=lambda: LoopDecision(
            action="stop" if values else "continue", reason="Observe once."
        ),
        execute=lambda: values.append(2),
        persist=lambda: saved.extend(values),
        scope=scope(),
        max_steps=1,
    )
    assert decision.action == "stop"
    assert saved == [2]


def test_reconciliation_never_reexecutes_operation_and_validates_recovered_output():
    invoked = []
    reconciled = []
    registry = CapabilityRegistry(agent_mode=True)

    def reconcile(context, inputs):
        reconciled.append((context.call_id, len(inputs.observations)))
        return CapabilityOutcome(details={"worker_call_id": context.call_id})

    registry.register(
        sink(operation=lambda context, inputs: invoked.append("run"), reconcile=reconcile)
    )
    result = registry.reconcile("consume", {"observations": []}, context(), scope())
    assert invoked == []
    assert reconciled == [("1" * 32, 0)]
    assert result["details"]["worker_call_id"] == "1" * 32
    assert tools().reconcile("consume", {"observations": []}, context(), scope()) is None


def test_snapshots_bind_version_schema_and_metadata_without_callable():
    registry = tools()
    snapshot = registry.snapshot("offline_rmse")
    baseline = AgentBaseline(
        **analysis_plan_data(),
        version=1,
        capability_snapshots={"offline_rmse": snapshot},
    )
    assert baseline.capability_snapshots["offline_rmse"].metadata.cacheable is True
    other = CapabilityRegistry(agent_mode=True)
    register_rmse(other, [], unit="cm")
    assert fingerprint(snapshot.model_dump(mode="json")) != fingerprint(
        other.snapshot("offline_rmse").model_dump(mode="json")
    )
    assert "operation" not in json.dumps(snapshot.model_dump(mode="json"))
    wrong = snapshot.model_copy(update={"name": "another"})
    with pytest.raises(ValueError, match="snapshot|name"):
        AgentBaseline(
            **analysis_plan_data(),
            version=1,
            capability_snapshots={"offline_rmse": wrong},
        )


def test_full_array_and_scalar_field_bindings_are_schema_checked_and_resolved():
    registry = tools()
    plan = bound_plan()
    validate_bindings(plan, registry)
    resolved = resolve_inputs(plan.nodes[1], {"compute": outcome()})
    assert resolved["observations"][0]["value"] == 2.0
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])
    registry.register(sink(input_model=ScalarInput))
    plan = bound_plan(["observations", 0, "value"], "value")
    validate_bindings(plan, registry)
    assert resolve_inputs(plan.nodes[1], {"compute": outcome()}) == {"value": 2.0}


@pytest.mark.parametrize(
    "path,input_name",
    [
        (["unknown"], "observations"),
        (["observations", "value"], "observations"),
        (["observations", 200], "observations"),
        (["observations", 0, "invented"], "observations"),
        (["observations"], "unknown"),
        (["observations", 0, "unit"], "observations"),
    ],
)
def test_preflight_rejects_unknown_fields_wrong_containers_and_schema_mismatch(path, input_name):
    with pytest.raises(ValueError):
        validate_bindings(bound_plan(path, input_name), tools())


def test_partial_input_preflight_never_invents_placeholder_values():
    registry = tools()
    bad = analysis_plan_data()
    bad["nodes"][0]["inputs"]["observed"] = []
    with pytest.raises(ValueError):
        validate_bindings(AgentPlan.model_validate(bad), registry)
    bad = analysis_plan_data()
    del bad["nodes"][0]["inputs"]["observed"]
    with pytest.raises(ValueError, match="required|missing"):
        validate_bindings(AgentPlan.model_validate(bad), registry)
    bad = analysis_plan_data()
    bad["nodes"][0]["tool_version"] = "2"
    with pytest.raises(ValueError, match="version"):
        validate_bindings(AgentPlan.model_validate(bad), registry)


def test_bindings_require_direct_not_just_transitive_dependencies():
    data = bound_plan().model_dump(mode="json")
    data["nodes"].append(
        {
            "id": "third",
            "tool": "consume",
            "tool_version": "1",
            "depends_on": ["consume"],
            "bindings": {"observations": {"node_id": "compute", "path": ["observations"]}},
        }
    )
    with pytest.raises(ValueError, match="direct"):
        AgentPlan.model_validate(data)
    data["nodes"][2]["depends_on"].append("compute")
    validate_bindings(AgentPlan.model_validate(data), tools())


@pytest.mark.parametrize("path", [["observations", -1], ["observations", True], ["x"] * 17])
def test_binding_paths_reject_unsafe_indices_and_excessive_depth(path):
    with pytest.raises(ValueError):
        OutputReference(node_id="compute", path=path)


def test_binding_inputs_cannot_be_supplied_twice():
    with pytest.raises(ValueError, match="binding"):
        bound_plan(inputs={"observations": []})


@pytest.mark.parametrize(
    "outputs,path", [({}, ["observations"]), ({"compute": outcome()}, ["observations", 1])]
)
def test_resolution_errors_are_explicit_and_never_empty_fallbacks(outputs, path):
    node = bound_plan(path).nodes[1]
    with pytest.raises(ValueError, match="[Bb]inding"):
        resolve_inputs(node, outputs)


def test_runtime_input_validation_rejects_real_null_instead_of_trusting_schema_preflight():
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])
    registry.register(sink(input_model=ScalarInput))
    plan = bound_plan(["observations", 0, "value"], "value")
    validate_bindings(plan, registry)
    invalid = CapabilityOutcome(
        observations=[
            Observation(
                node_id="compute",
                metric_id="rmse",
                label="RMSE",
                unit="m",
                valid=False,
                reason="No valid value.",
            )
        ]
    )
    resolved = resolve_inputs(plan.nodes[1], {"compute": invalid})
    with pytest.raises(ValueError):
        registry.invoke("consume", resolved, context(), scope())


def test_plugin_preflight_cannot_silently_disagree_with_confirmed_plan():
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])

    def reject(plan, node):
        raise ValueError("Delivery inputs differ from confirmed requirements")

    registry.register(sink(validate_plan=reject))
    with pytest.raises(ValueError, match="confirmed requirements"):
        validate_bindings(bound_plan(), registry)


@pytest.mark.parametrize("invalid", ["effect", "unavailable", "role", "kind", "location"])
def test_preflight_blocks_unavailable_or_unbound_resource_requirements(invalid):
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])
    resource = resource_data()
    data = bound_plan().model_dump(mode="json")
    data["resources"] = [resource]
    data["nodes"][1]["resource_bindings"] = {"data": resource["id"]}
    changes = {
        "metadata": CapabilityMetadata(
            resources=[ResourceRequirement(role="data", kind="signal_data", location="uploads")]
        )
    }
    if invalid == "effect":
        changes["effect"] = "write_delivery"
    elif invalid == "unavailable":
        changes["readiness"] = lambda resources: Availability(
            state="unavailable", reason="Controlled Worker is offline."
        )
    else:
        if invalid == "role":
            data["nodes"][1]["resource_bindings"] = {}
        else:
            data["resources"][0][invalid] = "wrong"
    registry.register(sink(**changes))
    with pytest.raises(ValueError):
        validate_bindings(AgentPlan.model_validate(data), registry)


def test_snapshot_does_not_share_mutable_metadata_with_registry():
    registry = tools()
    first = registry.snapshot("offline_rmse")
    first.metadata.metrics.clear()
    assert registry.snapshot("offline_rmse").metadata.metrics[0].id == "rmse"


@pytest.mark.parametrize("mutation", ["same-version-unit", "missing", "extra"])
def test_confirmed_baseline_requires_exact_current_declaration_snapshots(mutation):
    registry = tools()
    snapshots = {"offline_rmse": registry.snapshot("offline_rmse")}
    baseline = AgentBaseline(
        **analysis_plan_data(),
        version=1,
        confirmed=True,
        confirmed_at="2026-10-22T00:00:00Z",
        capability_snapshots=snapshots,
    )
    validate_bindings(baseline, registry)
    if mutation == "same-version-unit":
        registry = CapabilityRegistry(agent_mode=True)
        register_rmse(registry, [], unit="cm")
    elif mutation == "missing":
        baseline.capability_snapshots.clear()
    else:
        baseline.capability_snapshots["consume"] = registry.snapshot("consume")
    with pytest.raises(ValueError, match="snapshot|declaration"):
        validate_bindings(baseline, registry)


def test_numeric_table_rows_can_feed_registered_computation_with_real_validation():
    registry = tools()
    data = analysis_plan_data()
    data["nodes"].insert(
        0,
        {"id": "data", "tool": "consume", "tool_version": "1", "inputs": {"observations": []}},
    )
    data["nodes"][1]["inputs"] = {"reference": [0.0]}
    data["nodes"][1]["depends_on"] = ["data"]
    data["nodes"][1]["bindings"] = {
        "observed": {"node_id": "data", "path": ["tables", 0, "rows", 0]}
    }
    plan = AgentPlan.model_validate(data)
    validate_bindings(plan, registry)
    supplied = CapabilityOutcome(
        tables=[ResultTable(id="samples", columns=["sample"], rows=[[1.0]])]
    )
    inputs = resolve_inputs(plan.nodes[1], {"data": supplied})
    result = registry.invoke("offline_rmse", inputs, context(), scope())
    assert result["observations"][0]["value"] == 1.0
    nonnumeric = CapabilityOutcome(
        tables=[ResultTable(id="samples", columns=["sample"], rows=[["not a number"]])]
    )
    with pytest.raises(ValueError):
        registry.validate_inputs(
            "offline_rmse", resolve_inputs(plan.nodes[1], {"data": nonnumeric})
        )


@pytest.mark.parametrize("mutation", ["output", "criterion", "unit"])
def test_preflight_rejects_metrics_not_declared_by_the_producer(mutation):
    registry = tools()
    registry.register(sink(name="evaluate_criteria"))
    data = verification_plan_data()
    if mutation == "output":
        data["requirements"]["requested_outputs"][0]["item_id"] = "nonexistent"
    elif mutation == "criterion":
        data["requirements"]["criteria"][0]["metric"]["metric_id"] = "nonexistent"
    else:
        data["requirements"]["criteria"][0]["unit"] = "cm"
    with pytest.raises(ValueError, match="metric|unit"):
        validate_bindings(AgentPlan.model_validate(data), registry)
