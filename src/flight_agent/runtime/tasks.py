from collections.abc import Sequence
from typing import Annotated

from pydantic import Field, JsonValue, field_validator

from ..models import Contract
from .contracts import Name, finite_mapping


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
