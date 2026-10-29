"""Temporary v1 orchestration contracts; removed with the legacy runtime in Task 6."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract, Nonempty
from ..runtime.contracts import AnalysisRun, AnalysisSpec, EvidenceFile, Version
from ..runtime.tasks import GeneralBaseline, GeneralProposal, PlanNode, plan_ancestors
from .contracts import (
    METRICS,
    AnalysisKind,
    AnalysisResult,
    AnalysisRunRequest,
    Artifact,
    Criterion,
    Decision,
    Digest,
    Evaluation,
    Identifier,
    MutableRecord,
    NodeIdentifier,
    NodeStatus,
    ProjectDescriptor,
    RunRecord,
    SteadyStatePolicy,
    ToolCall,
    ToolName,
)
from .diagnosis_contracts import (
    DiagnosisBaseline,
    DiagnosisPlanRevision,
    DiagnosisProposal,
    DiagnosisTermination,
    DiagnosticRound,
    Hypothesis,
    HypothesisAssessment,
    PendingAnalysis,
)
from .diagnostic_data import DiagnosticProject


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
    tool_versions: dict[ToolName, Version] = {}

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


class ExecutionNode(MutableRecord):
    id: NodeIdentifier
    tool: ToolName
    depends_on: list[str] = []
    inputs: dict[str, JsonValue] = {}
    status: NodeStatus = "pending"
    error: str | None = None


class PlanVersion(Contract):
    baseline_version: int
    proposal: PlanProposal
    nodes: list[ExecutionNode]


class VerificationTask(MutableRecord):
    scenario: Literal["d1"] = "d1"
    id: Identifier
    project: ProjectDescriptor
    baseline: EvaluationBaseline
    proposal: PlanProposal
    baseline_history: list[EvaluationBaseline] = []
    plan_history: list[PlanVersion] = []
    nodes: list[ExecutionNode]
    calls: list[ToolCall] = []
    decisions: list[Decision] = []
    analysis_runs: list[AnalysisRun] = []
    evaluation_runs: list[AnalysisRun] = []
    pending_specs: dict[AnalysisKind, AnalysisSpec] = {}
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


class DiagnosisTask(MutableRecord):
    scenario: Literal["d2"] = "d2"
    id: Identifier
    project: DiagnosticProject
    baseline: DiagnosisBaseline
    proposal: DiagnosisProposal
    result_directory: Nonempty
    output_root: str | None = None
    stage: str = "awaiting_confirmation"
    nodes: list[ExecutionNode] = []
    calls: list[ToolCall] = []
    decisions: list[Decision] = []
    analysis_runs: list[AnalysisRun] = []
    plan_revisions: list[DiagnosisPlanRevision] = []
    rounds: list[DiagnosticRound] = []
    assessments: list[HypothesisAssessment] = []
    artifacts: list[Artifact] = []
    input_snapshot: DiagnosticProject | None = None
    pending_analysis: PendingAnalysis | None = None
    termination: DiagnosisTermination | None = None
    started_at: str | None = None
    dispatch_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 0
    no_progress_rounds: Annotated[int, Field(strict=True, ge=0)] = 0

    @field_validator("artifacts", mode="before")
    @classmethod
    def engine_evidence_artifacts(cls, value: list[object]) -> list[object]:
        return [
            Artifact(
                **item.model_dump(),
                media_type="application/json",
                role="record",
            )
            if isinstance(item, EvidenceFile) else item
            for item in value
        ]

    @property
    def current_hypotheses(self) -> list[Hypothesis]:
        for round_ in reversed(self.rounds):
            if revision := round_.review.hypothesis_revision:
                return revision.hypotheses
        return self.baseline.hypotheses


class GeneralTask(MutableRecord):
    scenario: Literal["general"] = "general"
    workflow: NodeIdentifier
    id: Identifier
    baseline: GeneralBaseline
    proposal: GeneralProposal
    result_directory: Nonempty
    output_root: str | None = None
    stage: str = "awaiting_confirmation"
    nodes: list[ExecutionNode]
    calls: list[ToolCall] = []
    decisions: list[Decision] = []
    analysis_runs: list[AnalysisRun] = []
    artifacts: list[Artifact] = []
    outputs: dict[str, dict[str, JsonValue]] = {}
    started_at: str | None = None


LegacyTask = VerificationTask | DiagnosisTask | GeneralTask
