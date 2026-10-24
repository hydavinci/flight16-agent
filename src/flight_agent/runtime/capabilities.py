from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Generic, TypeVar

from pydantic import BaseModel, JsonValue, TypeAdapter

from ..models import Contract, Nonempty
from .capability_metadata import Availability, CapabilityMetadata, CapabilitySnapshot
from .contracts import Effect, ExecutionScope, Name, Version, finite_mapping
from .observations import CapabilityOutcome, Finding, Observation, OutputArtifact, ResultTable
from .resources import ResourceReference

if TYPE_CHECKING:
    from .agent_contracts import AgentNode, AgentPlan, AgentScope

Context = TypeVar("Context")
CapabilityContext = TypeVar("CapabilityContext", contravariant=True)
Input = TypeVar("Input", bound=Contract)
Output = TypeVar("Output", bound=Contract)


@dataclass(frozen=True)
class Capability(Generic[CapabilityContext, Input, Output]):
    name: str
    version: str
    description: str
    input_model: type[Input]
    output_model: type[Output]
    effect: Effect
    operation: Callable[[CapabilityContext, Input], Output]
    metadata: CapabilityMetadata | None = None
    readiness: Callable[[tuple[ResourceReference, ...]], Availability] | None = None
    reconcile: Callable[[CapabilityContext, Input], Output | None] | None = None
    validate_plan: Callable[["AgentPlan", "AgentNode"], None] | None = None


class CapabilityDefinition(Contract):
    name: Name
    version: Version
    description: Nonempty
    effect: Effect


@dataclass(frozen=True)
class _Entry(Generic[Context]):
    definition: CapabilityDefinition
    input_model: type[Contract]
    output_model: type[Contract]
    operation: Callable[[Context, dict[str, JsonValue]], object]
    metadata: CapabilityMetadata | None
    readiness: Callable[[tuple[ResourceReference, ...]], Availability] | None
    reconcile: Callable[[Context, dict[str, JsonValue]], object] | None
    validate_plan: Callable[["AgentPlan", "AgentNode"], None] | None


def validate_output(model: type[BaseModel], value: object) -> dict[str, JsonValue]:
    if isinstance(value, BaseModel):
        value = value.model_dump(warnings=False)
    output = model.model_validate(value)
    return finite_mapping(output.model_dump(mode="json"))


class CapabilityRegistry(Generic[Context]):
    def __init__(self, *, agent_mode: bool = False) -> None:
        self._entries: dict[str, _Entry[Context]] = {}
        self._agent_mode = agent_mode

    def register(self, capability: Capability[Context, Input, Output]) -> None:
        definition = CapabilityDefinition(
            name=capability.name,
            version=capability.version,
            description=capability.description,
            effect=capability.effect,
        )
        if capability.name in self._entries:
            raise ValueError(f"Capability is already registered: {capability.name}")
        for model in (capability.input_model, capability.output_model):
            if not issubclass(model, Contract) or model.model_config.get("extra") != "forbid":
                raise ValueError("Capability schemas must be strict Contract subclasses")
        metadata = (
            CapabilityMetadata.model_validate(capability.metadata.model_dump(mode="json"))
            if capability.metadata is not None
            else None
        )
        if self._agent_mode or metadata is not None:
            if metadata is None:
                raise ValueError("Agent capabilities require metadata")
            if capability.output_model is not CapabilityOutcome:
                raise ValueError("Agent output_model must be exactly CapabilityOutcome")
            if capability.effect in ("write_candidate", "adopt_candidate"):
                raise ValueError("Candidate effects are unsupported for agent capabilities")

        def invoke(context: Context, inputs: dict[str, JsonValue]) -> object:
            validated = capability.input_model.model_validate(inputs)
            return capability.operation(context, validated)

        def reconcile(context: Context, inputs: dict[str, JsonValue]) -> object:
            validated = capability.input_model.model_validate(inputs)
            if capability.reconcile is None:
                raise ValueError("Capability has no reconciliation handler")
            return capability.reconcile(context, validated)

        self._entries[capability.name] = _Entry(
            definition,
            capability.input_model,
            capability.output_model,
            invoke,
            metadata,
            capability.readiness,
            reconcile if capability.reconcile is not None else None,
            capability.validate_plan,
        )

    def _entry(self, name: str) -> _Entry[Context]:
        if name not in self._entries:
            raise ValueError(f"Capability is not registered: {name}")
        return self._entries[name]

    def definition(self, name: str) -> CapabilityDefinition:
        return self._entry(name).definition

    def validate_inputs(self, name: str, inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return validate_output(self._entry(name).input_model, inputs)

    def validate_result(self, name: str, output: object) -> dict[str, JsonValue]:
        entry = self._entry(name)
        result = validate_output(entry.output_model, output)
        if entry.metadata is not None:
            validate_declared_outcome(entry.metadata, result)
        return result

    def snapshot(self, name: str) -> CapabilitySnapshot:
        entry = self._entry(name)
        if entry.metadata is None or entry.output_model is not CapabilityOutcome:
            raise ValueError("Agent capabilities require metadata and CapabilityOutcome")
        return CapabilitySnapshot(
            name=entry.definition.name,
            version=entry.definition.version,
            effect=entry.definition.effect,
            input_schema=entry.input_model.model_json_schema(),
            output_schema=entry.output_model.model_json_schema(),
            metadata=CapabilityMetadata.model_validate(entry.metadata.model_dump(mode="json")),
        )

    def availability(
        self, name: str, resources: tuple[ResourceReference, ...] = ()
    ) -> Availability:
        entry = self._entry(name)
        resources = tuple(ResourceReference.model_validate(resource) for resource in resources)
        if entry.metadata is not None:
            for requirement in entry.metadata.resources:
                if requirement.required and not any(
                    resource.kind == requirement.kind
                    and (requirement.location is None or resource.location == requirement.location)
                    for resource in resources
                ):
                    return Availability(
                        state="needs_input", reason=f"Missing resource role: {requirement.role}"
                    )
        if entry.readiness is None:
            return Availability(state="available")
        result = entry.readiness(resources)
        return Availability.model_validate(result.model_dump(mode="json"))

    def validate_plan(self, plan: "AgentPlan", node: "AgentNode") -> None:
        entry = self._entry(node.tool)
        if entry.validate_plan is not None:
            entry.validate_plan(plan, node)

    def validate_partial_inputs(
        self, name: str, inputs: dict[str, JsonValue], bound_fields: set[str]
    ) -> None:
        model = self._entry(name).input_model
        fields = model.model_fields
        if (set(inputs) | bound_fields) - fields.keys():
            raise ValueError("Unknown capability input field")
        for field_name, field in fields.items():
            if field_name in inputs:
                annotation = (
                    Annotated[field.annotation, *field.metadata]
                    if field.metadata
                    else field.annotation
                )
                TypeAdapter(annotation).validate_python(inputs[field_name])
            elif field.is_required() and field_name not in bound_fields:
                raise ValueError(f"Missing required capability input: {field_name}")
        if not bound_fields:
            self.validate_inputs(name, inputs)

    def catalog(self, resources: tuple[ResourceReference, ...] = ()) -> list[dict[str, object]]:
        return [
            {
                **entry.definition.model_dump(),
                "input_schema": entry.input_model.model_json_schema(),
                "output_schema": entry.output_model.model_json_schema(),
                **(
                    {
                        "metadata": entry.metadata.model_dump(mode="json"),
                        "availability": self.availability(
                            entry.definition.name, resources
                        ).model_dump(mode="json"),
                    }
                    if entry.metadata is not None
                    else {}
                ),
            }
            for entry in self._entries.values()
        ]

    def invoke(
        self,
        name: str,
        inputs: dict[str, JsonValue],
        context: Context,
        scope: "ExecutionScope | AgentScope",
    ) -> dict[str, JsonValue]:
        entry = self._entry(name)
        scope = revalidate_scope(scope)
        scope.authorize(entry.definition.effect)
        inputs = self.validate_inputs(name, inputs)
        from .agent_contracts import AgentScope

        if isinstance(scope, AgentScope):
            self.snapshot(name)
            available = self.availability(name, tuple(scope.resources))
            if available.state != "available":
                raise ValueError(available.reason)
        return self.validate_result(name, entry.operation(context, inputs))

    def reconcile(
        self,
        name: str,
        inputs: dict[str, JsonValue],
        context: Context,
        scope: "ExecutionScope | AgentScope",
    ) -> dict[str, JsonValue] | None:
        entry = self._entry(name)
        scope = revalidate_scope(scope)
        scope.authorize(entry.definition.effect)
        inputs = self.validate_inputs(name, inputs)
        from .agent_contracts import AgentScope

        if isinstance(scope, AgentScope):
            self.snapshot(name)
        if entry.reconcile is None:
            return None
        result = entry.reconcile(context, inputs)
        return None if result is None else self.validate_result(name, result)


def revalidate_scope(scope: "ExecutionScope | AgentScope") -> "ExecutionScope | AgentScope":
    from .agent_contracts import AgentScope

    if isinstance(scope, AgentScope):
        return AgentScope.model_validate(scope.model_dump(mode="json", warnings=False))
    if isinstance(scope, ExecutionScope):
        return ExecutionScope.model_validate(scope.model_dump(mode="json", warnings=False))
    raise ValueError("Execution requires a registered scope type")


def validate_declared_outcome(
    metadata: CapabilityMetadata, output: object, *, node_id: str | None = None
) -> CapabilityOutcome:
    metadata = CapabilityMetadata.model_validate(metadata)
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json", warnings=False)
    outcome = CapabilityOutcome.model_validate(output)
    metrics = {metric.id: metric for metric in metadata.metrics}
    for observation in outcome.observations:
        metric = metrics.get(observation.metric_id)
        if metric is None:
            raise ValueError(f"Undeclared metric: {observation.metric_id}")
        if observation.unit != metric.unit:
            raise ValueError(f"Metric unit differs from its declaration: {metric.id}")
        if (
            observation.valid
            and observation.special is not None
            and observation.special not in metric.comparable_specials
        ):
            raise ValueError(f"Metric special value is not declared comparable: {metric.id}")
    if node_id is not None:
        items: list[Observation | Finding | ResultTable | OutputArtifact] = [
            *outcome.observations,
            *outcome.findings,
            *outcome.tables,
            *outcome.artifacts,
        ]
        for item in items:
            if item.node_id is not None and item.node_id != node_id:
                raise ValueError("Capability output has a different producer node")
    return outcome
