from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract, Nonempty
from .contracts import AnalysisSpec, DesignContext, Effect, Name, ResourceBudget, finite_mapping


class PlanNode(Contract):
    id: Name
    tool: Name
    depends_on: Annotated[list[str], Field(max_length=8)] = []
    inputs: dict[str, JsonValue] = {}

    _finite = field_validator("inputs")(finite_mapping)


def plan_ancestors(nodes: Sequence[PlanNode]) -> dict[str, set[str]]:
    if not nodes:
        raise ValueError("A plan requires at least one node")
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("Plan node IDs must be unique")
    ancestors: dict[str, set[str]] = {}

    def visit(node_id: str, visiting: frozenset[str]) -> set[str]:
        if node_id in visiting:
            raise ValueError("Plan dependencies contain a cycle")
        if node_id not in by_id:
            raise ValueError("Plan dependency refers to an unknown node")
        if node_id in ancestors:
            return ancestors[node_id]
        dependencies = by_id[node_id].depends_on
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("Plan dependencies must be unique")
        result = set(dependencies)
        for dependency in dependencies:
            result.update(visit(dependency, visiting | {node_id}))
        ancestors[node_id] = result
        return result

    for node in nodes:
        visit(node.id, frozenset())
    return ancestors


class GeneralProposal(Contract):
    action: Literal["plan"] = "plan"
    title: Annotated[str, Field(min_length=1, max_length=200)]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    design: DesignContext
    nodes: Annotated[list[PlanNode], Field(min_length=1, max_length=40)]
    allowed_effects: tuple[Effect, ...] = ("read_only", "write_delivery")
    budget: ResourceBudget = ResourceBudget()

    @model_validator(mode="after")
    def bounded_plan(self) -> Self:
        plan_ancestors(self.nodes)
        if len(self.nodes) > self.budget.max_calls:
            raise ValueError("Plan exceeds the proposed call budget")
        if any(effect in ("write_candidate", "adopt_candidate") for effect in self.allowed_effects):
            raise ValueError("General tasks do not provide an isolated candidate/adoption scope")
        return self


class GeneralBaseline(Contract):
    version: Annotated[int, Field(strict=True, ge=1)]
    confirmed: Annotated[bool, Field(strict=True)] = False
    confirmed_at: str | None = None
    objective: Nonempty
    design: DesignContext
    specs: dict[Name, AnalysisSpec]
    allowed_effects: tuple[Effect, ...]
    budget: ResourceBudget
