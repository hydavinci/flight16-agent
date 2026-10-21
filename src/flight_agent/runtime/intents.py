from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract
from .contracts import Name, finite_mapping

Scenario = Literal["auto", "d1", "d2"]


class WorkflowRequest(Contract):
    workflow: Name
    request: Annotated[str, Field(min_length=1, max_length=4000)]
    inputs: dict[str, JsonValue] = {}

    _finite = field_validator("inputs")(finite_mapping)


class IntentProposal(Contract):
    action: Literal["answer", "clarify", "plan"]
    text: Annotated[str, Field(min_length=1, max_length=6000)]
    steps: Annotated[list[WorkflowRequest], Field(max_length=6)] = []

    @model_validator(mode="after")
    def consistent_steps(self) -> Self:
        if (self.action == "plan") != bool(self.steps):
            raise ValueError("Only a plan must contain workflow steps")
        return self
