from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StrictBool, model_validator

from ..models import FiniteNumber
from .contracts import Digest, Name, Version
from .resources import AgentContract, Text


class CriterionSource(AgentContract):
    kind: Literal["user_message", "standard_file", "accepted_example"]
    reference: Text
    quote: Text
    sha256: Digest | None = None
    version: Version | None = None

    @model_validator(mode="after")
    def file_identity(self) -> Self:
        if self.kind != "user_message" and self.sha256 is None:
            raise ValueError("File and example sources require their digest")
        return self


class MetricReference(AgentContract):
    node_id: Name
    metric_id: Name


class Criterion(AgentContract):
    id: Name
    metric: MetricReference
    operator: Literal["<", "<=", "==", ">=", ">"]
    threshold: FiniteNumber
    unit: Annotated[str, Field(min_length=1, max_length=100)]
    conditions: dict[str, JsonValue] = {}
    source: CriterionSource


class OutputRequirement(AgentContract):
    id: Name
    kind: Literal["metric", "finding", "table", "artifact"]
    node_id: Name | None = Field(
        default=None,
        description=(
            "ID of the plan node producing this output. Required for a fulfilled output; "
            "null is allowed only with an explicit plan.gaps entry for this requirement ID."
        ),
    )
    item_id: Name | None = Field(
        default=None,
        description=(
            "Metric, finding, table or artifact ID produced by node_id. Required for a fulfilled "
            "output; null requires an explicit plan.gaps entry for this requirement ID."
        ),
    )
    description: Text


class CapabilityGap(AgentContract):
    requirement_id: Name
    missing_capability: Text | None = None
    missing_inputs: Annotated[list[Text], Field(max_length=40)] = []
    limitation: Text
    next_step: Text


class TaskRequirements(AgentContract):
    objective: Text
    mode: Literal["analysis", "verification"]
    requested_outputs: Annotated[list[OutputRequirement], Field(min_length=1, max_length=80)]
    criteria: Annotated[list[Criterion], Field(max_length=80)] = []
    overall_requested: StrictBool = False
    delivery_formats: Annotated[list[Name], Field(max_length=8)] = []

    @model_validator(mode="after")
    def explicit_acceptance(self) -> Self:
        if self.mode == "verification" and not self.criteria:
            raise ValueError("Verification requires explicit criteria")
        if self.mode == "analysis" and (self.criteria or self.overall_requested):
            raise ValueError("Analysis must not imply an acceptance decision")
        for values in (self.requested_outputs, self.criteria):
            if len({item.id for item in values}) != len(values):
                raise ValueError("Requirement and criterion IDs must be unique")
        if len(set(self.delivery_formats)) != len(self.delivery_formats):
            raise ValueError("Delivery formats must be unique")
        return self
