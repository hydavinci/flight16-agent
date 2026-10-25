from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .capability_metadata import CapabilitySnapshot
from .contracts import (
    Digest,
    Effect,
    Name,
    ResourceBudget,
    RunId,
    Version,
    fingerprint,
    finite_mapping,
)
from .observations import CapabilityOutcome, OutputArtifact
from .requirements import CapabilityGap, TaskRequirements
from .resources import AgentContract, ResourceReference, Text
from .tasks import PlanNode, plan_ancestors

if TYPE_CHECKING:
    from .capabilities import CapabilityRegistry


class OutputReference(AgentContract):
    node_id: Name
    path: Annotated[
        list[
            Annotated[StrictStr, Field(min_length=1, max_length=200)]
            | Annotated[StrictInt, Field(ge=0)]
        ],
        Field(min_length=1, max_length=16),
    ]


class AgentNode(PlanNode, AgentContract):
    id: Name
    tool: Name
    tool_version: Version
    depends_on: Annotated[list[Name], Field(max_length=8)] = []
    inputs: dict[str, JsonValue] = {}
    bindings: dict[Name, OutputReference] = {}
    resource_bindings: dict[Name, Name] = {}

    @model_validator(mode="after")
    def explicit_bindings(self) -> Self:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Dependencies must be unique")
        if set(self.inputs) & set(self.bindings):
            raise ValueError("An input cannot also have a binding")
        if any(ref.node_id not in self.depends_on for ref in self.bindings.values()):
            raise ValueError("Bindings require a directly declared dependency")
        return self


class _PlanFields(AgentContract):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    requirements: TaskRequirements
    nodes: Annotated[list[AgentNode], Field(max_length=40)]
    evaluation_node: Name | None = None
    allowed_effects: tuple[Effect, ...] = ("read_only",)
    budget: ResourceBudget = Field(
        default=ResourceBudget(),
        description=(
            "Each node dispatch consumes one round and one call, including evaluation, "
            "delivery and cached nodes. max_rounds and max_calls must each cover all nodes; "
            "rounds are not parallel dependency layers. Nested analysis may consume more."
        ),
    )
    gaps: Annotated[list[CapabilityGap], Field(max_length=80)] = []

    @field_validator("budget", mode="before")
    @classmethod
    def revalidate_budget(cls, value: object) -> object:
        if isinstance(value, ResourceBudget):
            return value.model_dump(mode="json")
        return value

    def check_dispatch_budget(self) -> None:
        # Validate new proposals, not historical baselines whose evidence must remain readable.
        if len(self.nodes) > self.budget.max_rounds:
            raise PydanticCustomError(
                "plan_round_budget",
                "Plan exceeds its round budget: {nodes} nodes require at least {nodes} rounds, "
                "but the proposed budget has {rounds}",
                {"nodes": len(self.nodes), "rounds": self.budget.max_rounds},
            )

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("Plan node IDs must be unique")
        if len(set(self.allowed_effects)) != len(self.allowed_effects):
            raise ValueError("Permissions must be unique")
        if any(effect in ("write_candidate", "adopt_candidate") for effect in self.allowed_effects):
            raise ValueError("Candidate permissions are unsupported")
        if len(self.nodes) > self.budget.max_calls:
            raise ValueError("Plan exceeds its call budget")
        if not self.nodes and not self.gaps:
            raise ValueError("A plan needs executable nodes or explicit gaps")
        if self.nodes:
            plan_ancestors(self.nodes)
        gap_ids = [gap.requirement_id for gap in self.gaps]
        outputs = {item.id: item for item in self.requirements.requested_outputs}
        if len(set(gap_ids)) != len(gap_ids):
            raise ValueError("Gap IDs must be unique")
        if not set(gap_ids) <= set(outputs):
            raise ValueError("Gap refers to an unknown requirement")
        for output in outputs.values():
            if output.node_id is not None and output.node_id not in by_id:
                raise ValueError("Output refers to an unknown producer")
            if (output.node_id is None or output.item_id is None) and output.id not in gap_ids:
                raise PydanticCustomError(
                    "plan_output_producer",
                    "Requested output {output_id} requires node_id and item_id identifying its "
                    "producer, or an explicit gap",
                    {"output_id": output.id},
                )
        if any(item.metric.node_id not in by_id for item in self.requirements.criteria):
            raise ValueError("Criterion refers to an unknown producer")
        if self.requirements.mode == "analysis" and self.evaluation_node is not None:
            raise ValueError("Analysis must not specify an evaluation node")
        if self.requirements.mode == "verification" and self.evaluation_node not in by_id:
            raise ValueError("Verification requires a registered evaluation node")
        return self


class AgentPlanDraft(_PlanFields):
    resource_ids: Annotated[list[Name], Field(max_length=40)] = []

    @model_validator(mode="after")
    def referenced_resources(self) -> Self:
        self.check_dispatch_budget()
        if len(set(self.resource_ids)) != len(self.resource_ids):
            raise ValueError("Resource IDs must be unique")
        if any(
            resource not in self.resource_ids
            for node in self.nodes
            for resource in node.resource_bindings.values()
        ):
            raise ValueError("Node refers to an unknown resource")
        return self


class AgentPlan(_PlanFields):
    resources: Annotated[list[ResourceReference], Field(max_length=40)] = []

    @model_validator(mode="after")
    def referenced_resources(self) -> Self:
        ids = [item.id for item in self.resources]
        if len(set(ids)) != len(ids):
            raise ValueError("Resource IDs must be unique")
        if any(
            resource not in ids
            for node in self.nodes
            for resource in node.resource_bindings.values()
        ):
            raise ValueError("Node refers to an unknown resource")
        return self


class AgentBaseline(AgentPlan):
    version: Annotated[int, Field(strict=True, ge=1)]
    confirmed: StrictBool = False
    confirmed_at: str | None = None
    capability_snapshots: dict[Name, CapabilitySnapshot] = {}

    @model_validator(mode="after")
    def confirmation_record(self) -> Self:
        if self.confirmed != (self.confirmed_at is not None):
            raise ValueError("Confirmation requires its recorded timestamp")
        if any(name != snapshot.name for name, snapshot in self.capability_snapshots.items()):
            raise ValueError("Capability snapshot key must match its name")
        return self


class AgentScope(AgentContract):
    baseline_version: Annotated[int, Field(strict=True, ge=1)]
    confirmed: StrictBool
    resources: list[ResourceReference] = []
    allowed_effects: tuple[Effect, ...] = ("read_only",)

    @model_validator(mode="after")
    def no_candidates(self) -> Self:
        if any(effect in ("write_candidate", "adopt_candidate") for effect in self.allowed_effects):
            raise ValueError("Candidate permissions are unsupported")
        return self

    def authorize(self, effect: Effect) -> None:
        current = type(self).model_validate(self.model_dump(mode="json"))
        if not current.confirmed:
            raise ValueError("Capability execution requires a confirmed task baseline")
        if effect in ("write_candidate", "adopt_candidate"):
            raise ValueError("Candidate effects are unsupported")
        if effect not in current.allowed_effects:
            raise ValueError(f"Effect is outside the confirmed permissions: {effect}")


class InvocationSpec(AgentContract):
    capability: Name
    capability_version: Version
    inputs: dict[str, JsonValue]
    resource_fingerprints: dict[Name, Digest] = {}
    conditions: dict[str, JsonValue] = {}
    numerics: dict[str, JsonValue] = {}
    dependency_output_sha256: dict[Name, Digest] = {}

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class InvocationRun(AgentContract):
    schema_version: Literal["2.0"] = "2.0"
    id: RunId
    spec: InvocationSpec
    output: CapabilityOutcome
    output_sha256: Digest
    started_at: Text
    finished_at: Text
    artifacts: Annotated[list[OutputArtifact], Field(max_length=200)] = []
    reused_from: RunId | None = None

    @model_validator(mode="after")
    def verified_output(self) -> Self:
        if fingerprint(self.output.model_dump(mode="json")) != self.output_sha256:
            raise ValueError("Invocation output does not match its recorded digest")
        return self


class BudgetState(AgentContract):
    started_at: str | None = None
    calls: Annotated[int, Field(strict=True, ge=0)] = 0
    rounds: Annotated[int, Field(strict=True, ge=0)] = 0
    no_progress: Annotated[int, Field(strict=True, ge=0)] = 0
    round_charges: Annotated[list[Name], Field(max_length=20)] = []

    @model_validator(mode="after")
    def consistent_charges(self) -> Self:
        if len(set(self.round_charges)) != len(self.round_charges) or (
            len(self.round_charges) > self.rounds
        ):
            raise ValueError("Round charge identities must be unique and backed by the counter")
        return self


def resolve_inputs(node: AgentNode, outputs: dict[str, CapabilityOutcome]) -> dict[str, JsonValue]:
    node = AgentNode.model_validate(node)
    resolved = dict(node.inputs)
    for field, reference in node.bindings.items():
        if field in resolved or reference.node_id not in node.depends_on:
            raise ValueError("Binding conflicts with the confirmed input scope")
        if reference.node_id not in outputs:
            raise ValueError(f"Binding output is unavailable: {reference.node_id}")
        value: JsonValue = CapabilityOutcome.model_validate(outputs[reference.node_id]).model_dump(
            mode="json"
        )
        for part in reference.path:
            if isinstance(part, str) and isinstance(value, dict) and part in value:
                value = value[part]
            elif type(part) is int and isinstance(value, list) and 0 <= part < len(value):
                value = value[part]
            else:
                raise ValueError(
                    f"Binding path does not exist: {reference.node_id}/{reference.path}"
                )
        resolved[field] = value
    return finite_mapping(resolved)


def _schema_variants(
    schema: dict[str, JsonValue], root: dict[str, JsonValue], depth: int = 0
) -> list[dict[str, JsonValue]]:
    if depth > 20:
        raise ValueError("Binding schema exceeds its reference depth limit")
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise ValueError("Binding schemas require local declared references")
        definitions = root.get("$defs", {})
        target = definitions.get(reference[8:]) if isinstance(definitions, dict) else None
        if not isinstance(target, dict):
            raise ValueError("Binding schema reference is missing")
        return _schema_variants(target, root, depth + 1)
    variants = schema.get("anyOf", schema.get("oneOf"))
    if variants is not None:
        if not isinstance(variants, list) or not variants:
            raise ValueError("Binding schema union is invalid")
        result = []
        for variant in variants:
            if not isinstance(variant, dict):
                raise ValueError("Binding schema union must contain schemas")
            result.extend(_schema_variants(variant, root, depth + 1))
        return result
    return [schema]


def _schema_path(
    schema: dict[str, JsonValue], path: list[str | int], root: dict[str, JsonValue]
) -> list[dict[str, JsonValue]]:
    variants = _schema_variants(schema, root)
    if not path:
        return variants
    part, *remaining = path
    result = []
    for variant in variants:
        child: JsonValue = None
        if isinstance(part, str) and variant.get("type") == "object":
            properties = variant.get("properties", {})
            if isinstance(properties, dict):
                child = properties.get(part)
        elif type(part) is int and variant.get("type") == "array":
            maximum = variant.get("maxItems")
            if isinstance(maximum, int) and part >= maximum:
                raise ValueError("Binding index exceeds the declared array bounds")
            prefix = variant.get("prefixItems")
            child = (
                prefix[part]
                if isinstance(prefix, list) and part < len(prefix)
                else variant.get("items")
            )
        if isinstance(child, dict):
            result.extend(_schema_path(child, remaining, root))
    if not result:
        raise ValueError(f"Binding path is not a declared output field: {path}")
    return result


def _compatible_schema(
    source: dict[str, JsonValue],
    target: dict[str, JsonValue],
    source_root: dict[str, JsonValue],
    target_root: dict[str, JsonValue],
    depth: int = 0,
) -> bool:
    if depth > 20:
        raise ValueError("Binding schemas exceed their comparison depth limit")
    sources = _schema_variants(source, source_root)
    targets = _schema_variants(target, target_root)
    if len(sources) != 1 or len(targets) != 1:
        return any(
            _compatible_schema(left, right, source_root, target_root, depth + 1)
            for left in sources
            for right in targets
        )
    left, right = sources[0], targets[0]
    source_type, target_type = left.get("type"), right.get("type")
    if source_type is None and not left.get("properties"):
        return True
    if target_type is None and not right.get("properties"):
        return True
    if source_type != target_type and not (source_type == "integer" and target_type == "number"):
        return False
    if target_type == "array":
        left_items, right_items = left.get("items"), right.get("items")
        return (
            isinstance(left_items, dict)
            and isinstance(right_items, dict)
            and _compatible_schema(left_items, right_items, source_root, target_root, depth + 1)
        )
    if target_type == "object":
        left_fields, right_fields = left.get("properties", {}), right.get("properties", {})
        required = right.get("required", [])
        if not isinstance(left_fields, dict) or not isinstance(right_fields, dict):
            return False
        if not isinstance(required, list) or any(key not in left_fields for key in required):
            return False
        if right.get("additionalProperties") is False and set(left_fields) - set(right_fields):
            return False
        for key in left_fields.keys() & right_fields.keys():
            a, b = left_fields[key], right_fields[key]
            if (
                not isinstance(a, dict)
                or not isinstance(b, dict)
                or not _compatible_schema(a, b, source_root, target_root, depth + 1)
            ):
                return False
    return True


def validate_bindings(plan: AgentPlan, registry: "CapabilityRegistry") -> None:
    baseline = AgentBaseline.model_validate(plan) if isinstance(plan, AgentBaseline) else None
    plan = (
        AgentPlan.model_validate(
            {
                key: value
                for key, value in baseline.model_dump(mode="json").items()
                if key in AgentPlan.model_fields
            }
        )
        if baseline is not None
        else AgentPlan.model_validate(plan)
    )
    nodes = {node.id: node for node in plan.nodes}
    resources = {resource.id: resource for resource in plan.resources}
    snapshots = {node.tool: registry.snapshot(node.tool) for node in plan.nodes}
    if baseline is not None and (baseline.confirmed or baseline.capability_snapshots):
        if set(baseline.capability_snapshots) != set(snapshots):
            raise ValueError("Confirmed capability snapshots must cover exactly the plan")
        for name, snapshot in snapshots.items():
            if fingerprint(snapshot.model_dump(mode="json")) != fingerprint(
                baseline.capability_snapshots[name].model_dump(mode="json")
            ):
                raise ValueError(
                    f"Capability declaration differs from its confirmed snapshot: {name}"
                )
    metrics = {
        node.id: {metric.id: metric for metric in snapshots[node.tool].metadata.metrics}
        for node in plan.nodes
    }
    gaps = {gap.requirement_id for gap in plan.gaps}
    for output in plan.requirements.requested_outputs:
        if output.kind == "metric" and output.id not in gaps:
            if output.item_id not in metrics.get(output.node_id or "", {}):
                raise ValueError(
                    f"Requested metric is not declared by its producer: {output.item_id}"
                )
    for criterion in plan.requirements.criteria:
        metric = metrics[criterion.metric.node_id].get(criterion.metric.metric_id)
        if metric is None:
            raise ValueError(f"Criterion metric is not declared: {criterion.metric.metric_id}")
        if metric.unit != criterion.unit:
            raise ValueError(f"Criterion unit differs from its metric declaration: {metric.id}")
    for node in plan.nodes:
        target = snapshots[node.tool]
        if target.version != node.tool_version:
            raise ValueError(f"Capability version differs from the plan: {node.tool}")
        if target.effect not in plan.allowed_effects:
            raise ValueError(f"Capability effect is outside the plan permissions: {node.tool}")
        requirements = {item.role: item for item in target.metadata.resources}
        if set(node.resource_bindings) - set(requirements):
            raise ValueError("Capability resource bindings contain an undeclared role")
        for requirement in requirements.values():
            resource_id = node.resource_bindings.get(requirement.role)
            if resource_id is None:
                if requirement.required:
                    raise ValueError(f"Missing resource binding: {requirement.role}")
                continue
            resource = resources[resource_id]
            if resource.kind != requirement.kind or (
                requirement.location is not None and resource.location != requirement.location
            ):
                raise ValueError(f"Resource does not match its declaration: {requirement.role}")
        available = registry.availability(
            node.tool,
            tuple(resources[identifier] for identifier in node.resource_bindings.values()),
        )
        if available.state != "available":
            raise ValueError(available.reason)
        registry.validate_partial_inputs(node.tool, node.inputs, set(node.bindings))
        for field, reference in node.bindings.items():
            source = snapshots[nodes[reference.node_id].tool]
            sources = _schema_path(source.output_schema, reference.path, source.output_schema)
            targets = _schema_path(target.input_schema, [field], target.input_schema)
            if not any(
                _compatible_schema(left, right, source.output_schema, target.input_schema)
                for left in sources
                for right in targets
            ):
                raise ValueError(f"Binding output schema is incompatible with input: {field}")
        registry.validate_plan(plan, node)
