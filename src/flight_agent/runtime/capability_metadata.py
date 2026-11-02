from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StrictBool, model_validator

from .contracts import Effect, Name, Version
from .resources import AgentContract, Text


class MetricDefinition(AgentContract):
    id: Name
    label: Text
    unit: Text
    comparable_specials: list[Literal["positive_infinity", "negative_infinity"]] = []

    @model_validator(mode="after")
    def unique_specials(self) -> Self:
        if len(set(self.comparable_specials)) != len(self.comparable_specials):
            raise ValueError("Comparable special values must be unique")
        return self


class ResourceRequirement(AgentContract):
    role: Name
    kind: Name
    location: Name | None = None
    required: bool = True


class Availability(AgentContract):
    state: Literal["available", "needs_input", "unavailable"]
    reason: Annotated[str, Field(max_length=6000)] = ""

    @model_validator(mode="after")
    def explicit_reason(self) -> Self:
        if self.state != "available" and not self.reason.strip():
            raise ValueError("Unavailable capabilities require an explicit reason")
        return self


OutputKind = Literal["metric", "finding", "table", "artifact"]


class CapabilityPlanning(AgentContract):
    output_kinds: Annotated[list[OutputKind], Field(min_length=1, max_length=4)]
    depends_on: Annotated[list[Name], Field(max_length=80)] = []
    dynamic_dependencies: StrictBool = False

    @model_validator(mode="after")
    def unique_selection_declarations(self) -> Self:
        if len(set(self.output_kinds)) != len(self.output_kinds):
            raise ValueError("Planning output kinds must be unique")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Planning dependencies must be unique")
        return self


class CapabilityMetadata(AgentContract):
    metrics: Annotated[list[MetricDefinition], Field(max_length=200)] = []
    resources: Annotated[list[ResourceRequirement], Field(max_length=40)] = []
    conditions_schema: dict[str, JsonValue] = {}
    validity_policy: dict[str, JsonValue] = {}
    planning_notes: Annotated[str, Field(max_length=6000)] = ""
    cacheable: bool = False
    planning: CapabilityPlanning | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def unique_declarations(self) -> Self:
        if len({metric.id for metric in self.metrics}) != len(self.metrics):
            raise ValueError("Metric declarations must be unique")
        if len({resource.role for resource in self.resources}) != len(self.resources):
            raise ValueError("Resource roles must be unique")
        return self


class CapabilitySnapshot(AgentContract):
    name: Name
    version: Version
    effect: Effect
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    metadata: CapabilityMetadata
