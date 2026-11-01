import pytest
from pydantic import ValidationError

from flight_agent.runtime.capability_metadata import CapabilityMetadata, CapabilityPlanning
from flight_agent.runtime.planning_contracts import PlanningMetrics


def test_missing_planning_declaration_is_unknown_not_an_empty_capability():
    assert CapabilityMetadata().planning is None
    metadata = CapabilityMetadata(planning=CapabilityPlanning(output_kinds=["metric"]))
    assert metadata.planning.depends_on == []


def test_planning_contracts_reject_duplicate_and_unbounded_values():
    with pytest.raises(ValidationError):
        CapabilityPlanning(output_kinds=["metric", "metric"])
    with pytest.raises(ValidationError):
        CapabilityPlanning(output_kinds=["metric"], depends_on=["prepare", "prepare"])
    with pytest.raises(ValidationError):
        PlanningMetrics(planning_duration_seconds=float("nan"))
    with pytest.raises(ValidationError):
        PlanningMetrics(correction_round_count=2)


def test_catalog_only_metadata_does_not_change_execution_snapshots():
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.runtime.capability_metadata import CapabilitySnapshot
    from flight_agent.runtime.contracts import fingerprint
    from flight_agent.verification.diagnostic_planner import DiagnosticReviewer
    from flight_agent.verification.plugins import register_builtin_plugins
    from flight_agent.verification.resources import ResourceRegistry

    tools = CapabilityRegistry(agent_mode=True)
    register_builtin_plugins(
        tools, ResourceRegistry(), backend=None, diagnostic_reviewer=DiagnosticReviewer(),
    )
    catalog = {entry["name"]: entry for entry in tools.catalog()}
    assert catalog["matlab_stepinfo"]["metadata"]["planning"]["depends_on"] == [
        "matlab_execute_project",
    ]
    snapshot = tools.snapshot("matlab_stepinfo")
    old_payload = snapshot.model_dump(mode="json")
    assert "planning" not in old_payload["metadata"]
    restored = CapabilitySnapshot.model_validate(old_payload)
    assert fingerprint(restored.model_dump(mode="json")) == fingerprint(old_payload)
    assert restored == snapshot
