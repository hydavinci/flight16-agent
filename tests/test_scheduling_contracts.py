from dataclasses import replace

import pytest

from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import CapabilityMetadata
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata, validate_scheduling
from tests.agent_helpers import RmseInput


def capability(effect="read_only", scheduling=None):
    return Capability(
        name="synthetic",
        version="1",
        description="Synthetic bounded operation.",
        input_model=RmseInput,
        output_model=CapabilityOutcome,
        effect=effect,
        operation=lambda context, inputs: CapabilityOutcome(),
        metadata=CapabilityMetadata(),
        scheduling=scheduling,
    )


@pytest.mark.parametrize(
    "effect,lane",
    [
        ("execute_project", "local_matlab"),
        ("execute_project", "remote_worker"),
        ("read_only", "read_only"),
        ("write_delivery", "read_only"),
    ],
)
def test_declared_effect_and_lane_are_validated(effect, lane):
    registry = CapabilityRegistry(agent_mode=True)
    registry.register(capability(effect, SchedulingMetadata(lane)))
    assert registry.scheduling("synthetic").lane == lane


@pytest.mark.parametrize(
    "effect,lane",
    [
        ("execute_project", "read_only"),
        ("read_only", "local_matlab"),
        ("write_delivery", "remote_worker"),
        ("write_candidate", "read_only"),
    ],
)
def test_effect_cannot_downgrade_or_invent_an_execution_lane(effect, lane):
    with pytest.raises(ValueError, match="scheduling"):
        validate_scheduling(effect, SchedulingMetadata(lane))


def test_execute_project_requires_a_declaration_and_invalid_lanes_fail_closed():
    with pytest.raises(ValueError, match="scheduling"):
        validate_scheduling("execute_project", None)
    with pytest.raises(ValueError, match="scheduling"):
        validate_scheduling("execute_project", SchedulingMetadata("planning"))


def test_read_only_default_does_not_change_snapshot_or_receipt_identity():
    first, second = CapabilityRegistry(agent_mode=True), CapabilityRegistry(agent_mode=True)
    original = capability()
    first.register(original)
    second.register(replace(original, scheduling=SchedulingMetadata("read_only")))
    expected = first.snapshot("synthetic").model_dump(mode="json")
    assert second.snapshot("synthetic").model_dump(mode="json") == expected
    actual = second.snapshot("synthetic").model_dump(mode="json")
    assert fingerprint(actual) == fingerprint(expected)
    assert "scheduling" not in second.catalog()[0]
    assert "scheduling" not in second.snapshot("synthetic").metadata.model_dump()
