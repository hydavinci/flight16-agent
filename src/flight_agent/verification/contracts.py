import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from ..models import Contract, FiniteNumber, Nonempty
from ..upload_models import finite_json

AnalysisKind = Literal["step_response", "stability_margins"]
EvaluationStatus = Literal["pass", "fail", "undetermined"]
SpecialValue = Literal["positive_infinity", "negative_infinity", "undefined"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]

METRICS: dict[str, tuple[AnalysisKind, str, str]] = {
    "overshoot": ("step_response", "<=", "%"),
    "settling_time": ("step_response", "<=", "s"),
    "gain_margin": ("stability_margins", ">=", "dB"),
    "phase_margin": ("stability_margins", ">=", "deg"),
}


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(char in value for char in ("\\", ":", "\0", "\r", "\n"))
    ):
        raise ValueError("Expected a relative path inside the project, without traversal")
    return value


def matlab_identifier(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", value):
        raise ValueError("Expected a simple MATLAB identifier, not an expression")
    return value


class SignalReference(Contract):
    name: Nonempty
    unit: Nonempty


class SteadyStatePolicy(Contract):
    tail_fraction: Annotated[FiniteNumber, Field(gt=0, le=0.5)] = 0.2
    minimum_window_seconds: Annotated[FiniteNumber, Field(gt=0)] = 1.0
    maximum_relative_range: Annotated[FiniteNumber, Field(gt=0, le=0.05)] = 0.02
    maximum_relative_drift: Annotated[FiniteNumber, Field(gt=0, le=0.05)] = 0.01


class NativeModule(Contract):
    name: Nonempty
    directory: Nonempty
    source: str | None = None

    _name = field_validator("name")(matlab_identifier)
    _directory = field_validator("directory")(relative_path)

    @field_validator("source")
    @classmethod
    def safe_source(cls, value: str | None) -> str | None:
        return relative_path(value) if value is not None else None


class ProjectConfig(Contract):
    schema_version: Literal["1.0"] = "1.0"
    name: Nonempty
    backend: Nonempty = "simulink"
    model: Nonempty
    initializer: Nonempty
    initializer_kind: Literal["script", "function"] = "function"
    working_directory: str | None = None
    dependencies: list[str] = []
    supported_trim_points: Annotated[list[int], Field(min_length=1)] = [1]
    trim_selector: str | None = None
    trim_selection_variable: str | None = None
    trim_selection_field: str = "PointIndex"
    input_signal: SignalReference
    response_signal: SignalReference
    operating_point_mode: Literal["initialized", "workspace"] = "initialized"
    operating_point_variable: str | None = None
    feedback_sign: Literal[-1, 1] = 1
    native_modules: list[NativeModule] = []
    steady_state: SteadyStatePolicy = SteadyStatePolicy()
    matlab_release: str | None = None

    _resources = field_validator("model", "initializer")(relative_path)
    _identifiers = field_validator(
        "trim_selection_variable", "trim_selection_field", "operating_point_variable"
    )(matlab_identifier)

    @field_validator("dependencies")
    @classmethod
    def safe_dependencies(cls, values: list[str]) -> list[str]:
        return [relative_path(value) for value in values]

    @field_validator("trim_selector", "working_directory")
    @classmethod
    def safe_selector(cls, value: str | None) -> str | None:
        return relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def valid_entries(self) -> Self:
        for value, suffix in ((self.model, ".slx"), (self.initializer, ".m")):
            path = PurePosixPath(value)
            if path.suffix != suffix:
                raise ValueError(f"Expected {suffix} entry: {value}")
            matlab_identifier(path.stem)
        if self.trim_selector is not None:
            path = PurePosixPath(self.trim_selector)
            if path.suffix != ".m":
                raise ValueError("Trim selector must be an existing .m function")
            matlab_identifier(path.stem)
        if self.operating_point_mode == "workspace" and not self.operating_point_variable:
            raise ValueError("A registered workspace operating point variable is required")
        if len(set(self.supported_trim_points)) != len(self.supported_trim_points) or any(
            point < 1 for point in self.supported_trim_points
        ):
            raise ValueError("Supported trim points must be unique positive integers")
        return self


class FileDigest(Contract):
    path: Nonempty
    sha256: Digest
    size: Annotated[int, Field(ge=0)]

    _path = field_validator("path")(relative_path)


class ProjectDescriptor(Contract):
    root: Nonempty
    fingerprint: Digest
    config: ProjectConfig
    files: list[FileDigest]
    capabilities: list[AnalysisKind] = ["step_response", "stability_margins"]
    warnings: list[str] = []


class Criterion(Contract):
    metric: Nonempty
    operator: Literal["<=", ">="]
    threshold: FiniteNumber
    unit: Nonempty

    @model_validator(mode="after")
    def supported_metric(self) -> Self:
        definition = METRICS.get(self.metric)
        if definition is None:
            raise ValueError(f"Unsupported verification metric: {self.metric}")
        if (self.operator, self.unit) != definition[1:]:
            raise ValueError(f"{self.metric} requires {definition[1]} in {definition[2]}")
        if self.metric in ("overshoot", "settling_time") and self.threshold < 0:
            raise ValueError(f"{self.metric} threshold cannot be negative")
        return self


class EvaluationBaseline(Contract):
    version: Annotated[int, Field(ge=1)]
    project_fingerprint: Digest
    trim_point: Annotated[int, Field(ge=1)]
    analyses: Annotated[list[AnalysisKind], Field(min_length=1)]
    criteria: Annotated[list[Criterion], Field(min_length=1)]
    confirmed: bool = False
    confirmed_at: str | None = None
    steady_state: SteadyStatePolicy = SteadyStatePolicy()

    @model_validator(mode="after")
    def complete_criteria(self) -> Self:
        if len(set(self.analyses)) != len(self.analyses):
            raise ValueError("Analysis kinds must be unique")
        metrics = [criterion.metric for criterion in self.criteria]
        if len(set(metrics)) != len(metrics):
            raise ValueError("Criteria must have unique metrics")
        required = {name for name, definition in METRICS.items() if definition[0] in self.analyses}
        if set(metrics) != required:
            raise ValueError("Criteria must cover exactly the selected analyses: " + str(required))
        return self


class MetricValue(Contract):
    value: FiniteNumber | None = None
    unit: Nonempty
    special: SpecialValue | None = None
    valid: bool = True
    reason: str = ""

    @model_validator(mode="after")
    def explicit_value(self) -> Self:
        if self.value is not None and self.special is not None:
            raise ValueError("A metric cannot contain both a finite and a special value")
        if (
            self.valid
            and self.value is None
            and self.special not in ("positive_infinity", "negative_infinity")
        ):
            raise ValueError("A valid metric requires a finite value or explicit infinity")
        if not self.valid and not self.reason:
            raise ValueError("An invalid metric requires an explicit reason")
        return self


class Artifact(FileDigest):
    media_type: Nonempty
    role: Literal["figure", "data", "report", "record"]


class AnalysisRecord(Contract):
    kind: AnalysisKind
    execution_status: Literal["completed", "failed"] = "completed"
    valid: bool
    metrics: dict[str, MetricValue]
    diagnostics: list[str] = []
    details: dict[str, JsonValue] = {}
    artifact_paths: list[str] = []

    @field_validator("artifact_paths")
    @classmethod
    def safe_artifacts(cls, values: list[str]) -> list[str]:
        return [relative_path(value) for value in values]

    @field_validator("details")
    @classmethod
    def finite_details(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        finite_json(value)
        return value

    @model_validator(mode="after")
    def failure_is_invalid(self) -> Self:
        if self.execution_status == "failed" and self.valid:
            raise ValueError("A failed analysis cannot be valid")
        return self


class AnalysisResult(Contract):
    run_id: Identifier
    project_fingerprint: Digest
    trim_point: Annotated[int, Field(ge=1)]
    matlab_version: Nonempty
    analyses: list[AnalysisRecord]
    artifacts: list[Artifact] = []
    provenance: dict[str, JsonValue] = {}

    @model_validator(mode="after")
    def unique_results(self) -> Self:
        names = [record.kind for record in self.analyses]
        paths = [artifact.path for artifact in self.artifacts]
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            raise ValueError("Analysis kinds and artifact paths must be unique")
        finite_json(self.provenance)
        return self

    def analysis(self, kind: AnalysisKind) -> AnalysisRecord | None:
        return next((item for item in self.analyses if item.kind == kind), None)


class AnalysisRunRequest(Contract):
    id: Identifier
    project_root: Nonempty
    project_fingerprint: Digest
    trim_point: Annotated[int, Field(strict=True, ge=1)]
    analyses: Annotated[list[AnalysisKind], Field(min_length=1)]
    trusted: Annotated[bool, Field(strict=True)]
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=1800)] = 300

    @model_validator(mode="after")
    def authorized_request(self) -> Self:
        if not self.trusted:
            raise ValueError("Explicit trusted-project consent is required")
        if len(set(self.analyses)) != len(self.analyses):
            raise ValueError("Requested analyses must be unique")
        return self


class RunRecord(Contract):
    id: Identifier
    request_sha256: Digest
    status: Literal["queued", "running", "completed", "failed", "interrupted"] = "queued"
    stage: str = "queued"
    created_at: Nonempty
    updated_at: Nonempty
    result: AnalysisResult | None = None
    error: str | None = None
    log: str = ""


class InspectProjectRequest(Contract):
    root: Nonempty


class MetricCheck(Contract):
    analysis: AnalysisKind
    metric: str
    value: float | None
    special: SpecialValue | None = None
    value_unit: str | None
    operator: str
    threshold: float
    unit: str
    status: EvaluationStatus
    reason: str
    evidence: list[str]


class Evaluation(Contract):
    status: EvaluationStatus
    baseline_version: int
    checks: list[MetricCheck]
    by_analysis: dict[AnalysisKind, EvaluationStatus]
    warnings: list[str] = []


NodeIdentifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ToolName = NodeIdentifier
NodeStatus = Literal["pending", "running", "completed", "failed", "interrupted"]


class PlanNode(Contract):
    id: NodeIdentifier
    tool: ToolName
    depends_on: Annotated[list[str], Field(max_length=8)] = []
    inputs: dict[str, JsonValue] = {}

    @field_validator("inputs")
    @classmethod
    def finite_inputs(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        finite_json(value)
        return value


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


class PlanProposal(Contract):
    action: Literal["plan", "clarify", "explain"]
    title: Annotated[str, Field(max_length=200)] = ""
    analyses: list[AnalysisKind] = []
    criteria: list[Criterion] = []
    trim_point: Literal[1] = 1
    grouping: Literal["combined", "separate"] = "combined"
    rationale: Annotated[str, Field(max_length=2000)] = ""
    question: Annotated[str, Field(max_length=2000)] = ""
    nodes: Annotated[list[PlanNode], Field(max_length=8)] = []

    @model_validator(mode="after")
    def executable_plan(self) -> Self:
        if self.action != "plan":
            if self.action == "clarify" and not self.question.strip():
                raise ValueError("A clarification must contain a question")
            if self.nodes or self.analyses or self.criteria:
                raise ValueError("Only a plan can contain executable nodes and criteria")
            return self
        if not self.title.strip() or not self.rationale.strip():
            raise ValueError("A plan requires a title and rationale")
        EvaluationBaseline(
            version=1,
            project_fingerprint="0" * 64,
            trim_point=self.trim_point,
            analyses=self.analyses,
            criteria=self.criteria,
        )
        plan_ancestors(self.nodes)
        return self


class EvidenceExplanation(Contract):
    text: Annotated[str, Field(min_length=1, max_length=6000)]
    evidence: Annotated[list[str], Field(min_length=1, max_length=4)]


class MutableRecord(Contract):
    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


class ExecutionNode(MutableRecord):
    id: NodeIdentifier
    tool: ToolName
    depends_on: list[str] = []
    inputs: dict[str, JsonValue] = {}
    status: NodeStatus = "pending"
    error: str | None = None


class ToolCall(MutableRecord):
    id: Identifier
    node_id: str
    tool: ToolName
    baseline_version: int
    status: NodeStatus = "running"
    started_at: str
    finished_at: str | None = None
    inputs: dict[str, JsonValue] = {}
    output: dict[str, JsonValue] = {}
    error: str | None = None


class PlanVersion(Contract):
    baseline_version: int
    proposal: PlanProposal
    nodes: list[ExecutionNode]


class PendingDecision(Contract):
    kind: Literal["clarify", "confirm_baseline", "confirm_replan", "resume", "retry"]
    message: Nonempty


class Decision(Contract):
    action: Literal["confirm_baseline", "confirm_replan", "resume", "retry"]
    baseline_version: int
    created_at: Nonempty
    trusted: bool = False


class ConversationMessage(Contract):
    role: Literal["user", "assistant"]
    content: Nonempty
    created_at: Nonempty


class VerificationTask(MutableRecord):
    id: Identifier
    project: ProjectDescriptor
    baseline: EvaluationBaseline
    proposal: PlanProposal
    baseline_history: list[EvaluationBaseline] = []
    plan_history: list[PlanVersion] = []
    nodes: list[ExecutionNode]
    calls: list[ToolCall] = []
    decisions: list[Decision] = []
    result: AnalysisResult | None = None
    evaluation: Evaluation | None = None
    artifacts: list[Artifact] = []
    result_directory: Nonempty
    output_root: str | None = None
    stage: str = "awaiting_confirmation"
    explanation: EvidenceExplanation | None = None
    explanation_error: str | None = None
    pending_request: AnalysisRunRequest | None = None
    worker_run: RunRecord | None = None
    replan_attempts: int = 0
    failure_proposal: PlanProposal | None = None
    replan_error: str | None = None


ConversationStatus = Literal[
    "planning", "waiting_for_user", "running", "completed", "failed", "interrupted"
]


class ConversationState(Contract):
    status: ConversationStatus
    pending_decision: PendingDecision | None = None
    error: str | None = None


class Conversation(MutableRecord):
    id: Identifier
    created_at: Nonempty
    updated_at: Nonempty
    status: ConversationStatus = "waiting_for_user"
    project_root: str | None = None
    output_root: str | None = None
    task: VerificationTask | None = None
    archived_tasks: list[VerificationTask] = []
    pending_decision: PendingDecision | None = None
    messages: list[ConversationMessage] = []
    error: str | None = None
    turn_origin: ConversationState | None = None

    def restore_turn_origin(self) -> bool:
        if self.turn_origin is None:
            return False
        self.status = self.turn_origin.status
        self.pending_decision = self.turn_origin.pending_decision
        self.error = self.turn_origin.error
        self.turn_origin = None
        return True
