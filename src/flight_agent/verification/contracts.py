import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract, FiniteNumber, Nonempty, finite_json
from ..runtime.agent_contracts import (
    AgentBaseline,
    AgentNode,
    BudgetState,
    InvocationRun,
    InvocationSpec,
)
from ..runtime.contracts import MutableRecord as MutableRecord
from ..runtime.contracts import Name
from ..runtime.contracts import ToolCall as ToolCall
from ..runtime.observations import CapabilityOutcome, OutputArtifact, TaskResult
from ..runtime.observations import MetricValue as MetricValue
from ..runtime.requirements import CapabilityGap

AnalysisKind = Literal["step_response", "stability_margins"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]



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


NodeIdentifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
NodeStatus = Literal["pending", "running", "completed", "failed", "interrupted"]


class PendingDecision(Contract):
    kind: Literal[
        "clarify", "capability_gap", "confirm_baseline", "resume", "retry",
    ]
    message: Nonempty


class Decision(Contract):
    action: Literal["confirm_baseline", "resume", "retry"]
    baseline_version: int
    created_at: Nonempty
    trusted: bool = False


class ConversationMessage(Contract):
    role: Literal["user", "assistant"]
    content: Nonempty
    created_at: Nonempty
    response_kind: Literal["answer", "clarify", "capability_gap", "plan"] | None = None
    gaps: Annotated[list[CapabilityGap], Field(max_length=40)] = []


class AgentExecutionNode(AgentNode, MutableRecord):
    status: NodeStatus = "pending"
    error: str | None = None


class AgentTask(MutableRecord):
    schema_version: Literal["2.0"] = "2.0"
    id: Identifier
    baseline: AgentBaseline
    baseline_sha256: Digest | None = None
    nodes: list[AgentExecutionNode]
    calls: list[ToolCall] = []
    decisions: list[Decision] = []
    outputs: dict[str, CapabilityOutcome] = {}
    analysis_runs: list[InvocationRun] = []
    pending_specs: dict[Identifier, InvocationSpec] = {}
    budget_state: BudgetState = BudgetState()
    progress_calls: list[Identifier] = []
    checkpoints: dict[Identifier, Nonempty] = {}
    checkpoint_sha256: dict[Identifier, Digest] = {}
    reuse_sources: dict[Identifier, Identifier] = {}
    reuse_mappings: dict[Identifier, dict[NodeIdentifier, NodeIdentifier]] = {}
    result: TaskResult | None = None
    artifacts: list[OutputArtifact] = []
    result_directory: Nonempty
    output_root: str | None = None
    stage: str = "awaiting_confirmation"

    @field_validator("checkpoints")
    @classmethod
    def safe_checkpoints(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value.values():
            relative_path(path)
        return value


Task = AgentTask


ConversationStatus = Literal[
    "planning", "waiting_for_user", "running", "completed", "failed", "interrupted"
]


class ConversationState(Contract):
    status: ConversationStatus
    pending_decision: PendingDecision | None = None
    error: str | None = None


class Conversation(MutableRecord):
    schema_version: Literal["2.0"] = "2.0"
    id: Identifier
    created_at: Nonempty
    updated_at: Nonempty
    status: ConversationStatus = "waiting_for_user"
    project_root: str | None = None
    resource_location: Name | None = None
    output_root: str | None = None
    output_root_reset: bool = False
    task: AgentTask | None = None
    archived_tasks: list[AgentTask] = []
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
