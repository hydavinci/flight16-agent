import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import Field, ValidationError, model_validator

from ..runtime.capability_metadata import CapabilityMetadata
from ..runtime.contracts import Name
from ..runtime.requirements import CriterionSource
from ..runtime.resources import AgentContract, ResourceReference
from .contracts import Conversation


@dataclass(frozen=True)
class PlanningIntent:
    metric_ids: frozenset[str] | None


class SourceScope(AgentContract):
    metric_ids: Annotated[list[Name], Field(max_length=200)] = []
    capability_names: Annotated[list[Name], Field(max_length=80)] = []

    @model_validator(mode="after")
    def complete_nonempty_scope(self) -> Self:
        if not self.metric_ids and not self.capability_names:
            raise ValueError("Empty source scope cannot prove irrelevance")
        if len(set(self.metric_ids)) != len(self.metric_ids):
            raise ValueError("Source metric scope must be unique")
        if len(set(self.capability_names)) != len(self.capability_names):
            raise ValueError("Source capability scope must be unique")
        return self


@dataclass(frozen=True)
class ContextSelection:
    catalog: tuple[dict[str, object], ...]
    sources: tuple[CriterionSource, ...]


@dataclass
class PlanningState:
    started_at: float
    serialized_bytes: int = 0
    selected_capability_count: int = 0
    selected_source_excerpt_count: int = 0
    correction_round_count: int = 0


def context_byte_size(context: dict[str, object]) -> int:
    return len(json.dumps(context, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def normalize_request(
    message: str, conversation: Conversation, catalog: list[dict[str, object]]
) -> PlanningIntent:
    user_messages = [item.content for item in conversation.messages if item.role == "user"]
    if conversation.task is not None or len(user_messages) > 1:
        return PlanningIntent(None)
    if user_messages and user_messages[0] != message:
        return PlanningIntent(None)
    match = re.fullmatch(
        r"(?:Report|Compute|Analyze|报告|计算|分析)\s+([^.!。\n]+)[.!。]?",
        message.strip(), re.IGNORECASE,
    )
    if match is None:
        return PlanningIntent(None)
    aliases: dict[str, set[str]] = {}
    for item in catalog:
        metadata = CapabilityMetadata.model_validate(item.get("metadata", {}))
        for metric in metadata.metrics:
            for alias in (metric.id, metric.label):
                aliases.setdefault(alias.casefold(), set()).add(metric.id)
    identifiers: set[str] = set()
    for token in re.split(r"\s+(?:and|和)\s+|[,、]", match[1], flags=re.IGNORECASE):
        options = aliases.get(token.strip().casefold(), set())
        if len(options) != 1:
            return PlanningIntent(None)
        identifiers.update(options)
    return PlanningIntent(frozenset(identifiers) if identifiers else None)


def _candidate_names(
    intent: PlanningIntent, catalog: list[dict[str, object]],
) -> set[str]:
    entries = {str(item["name"]): item for item in catalog}
    if len(entries) != len(catalog):
        raise ValueError("Capability catalog contains duplicate names")
    metadata = {
        name: CapabilityMetadata.model_validate(item.get("metadata", {}))
        for name, item in entries.items()
    }
    all_names = set(entries)
    if intent.metric_ids is None or any(item.planning is None for item in metadata.values()):
        return all_names
    selected = {
        name for name, item in metadata.items()
        if item.planning is not None and "metric" in item.planning.output_kinds
        and intent.metric_ids.intersection(metric.id for metric in item.metrics)
    }
    if not intent.metric_ids <= {
        metric.id for name in selected for metric in metadata[name].metrics
    }:
        return all_names
    pending = list(sorted(selected))
    while pending:
        name = pending.pop()
        declaration = metadata[name].planning
        if declaration is None or declaration.dynamic_dependencies:
            return all_names
        for dependency in declaration.depends_on:
            if dependency not in entries:
                return all_names
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def _resource_rank(
    item: dict[str, object], resources: dict[str, ResourceReference]
) -> tuple[int, str, str]:
    metadata = CapabilityMetadata.model_validate(item.get("metadata", {}))
    missing = sum(
        1 for role in metadata.resources
        if role.required and not any(
            resource.kind == role.kind
            and (role.location is None or resource.location == role.location)
            for resource in resources.values()
        )
    )
    return missing, str(item["name"]), str(item["version"])


def _source_scope(
    source: CriterionSource, resources: dict[str, ResourceReference]
) -> SourceScope | None:
    owner = resources.get(source.reference.split("#", 1)[0])
    if owner is None:
        return None
    hints = owner.metadata.get("planning_source_scopes")
    if not isinstance(hints, dict) or source.reference not in hints:
        return None
    try:
        return SourceScope.model_validate(hints[source.reference])
    except ValidationError:
        return None


def select_context(
    message: str, conversation: Conversation, *,
    catalog: list[dict[str, object]], resources: dict[str, ResourceReference],
    sources: dict[str, CriterionSource],
) -> ContextSelection:
    intent = normalize_request(message, conversation, catalog)
    selected = _candidate_names(intent, catalog)
    known_metrics = {
        metric.id for item in catalog
        for metric in CapabilityMetadata.model_validate(item.get("metadata", {})).metrics
    }
    known_names = {str(item["name"]) for item in catalog}
    # A full-catalog fallback is not proof that any associated excerpt is irrelevant.
    metric_scope = intent.metric_ids if selected != known_names else None
    excerpts: list[CriterionSource] = []
    for reference in sorted(sources):
        source = sources[reference]
        scope = _source_scope(source, resources)
        uncertain_scope = (
            scope is None
            or not set(scope.metric_ids) <= known_metrics
            or not set(scope.capability_names) <= known_names
        )
        if (
            metric_scope is None or source.kind == "user_message" or uncertain_scope
            or scope is not None and (
                bool(metric_scope.intersection(scope.metric_ids))
                or bool(selected.intersection(scope.capability_names))
            )
        ):
            excerpts.append(CriterionSource.model_validate(source.model_dump(mode="json")))
    return ContextSelection(
        catalog=tuple(deepcopy(item) for item in sorted(
            catalog, key=lambda entry: _resource_rank(entry, resources)
        ) if str(item["name"]) in selected),
        sources=tuple(excerpts),
    )
