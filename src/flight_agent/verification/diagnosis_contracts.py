from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract, Nonempty
from ..runtime.contracts import (
    AnalysisRun,
    AnalysisSpec,
    DesignContext,
    Digest,
    Name,
    PlanRevision,
    ResourceBudget,
    RunId,
    Version,
    finite_mapping,
)
from .diagnostic_data import DiagnosticOutput

Reason = Annotated[str, Field(min_length=1, max_length=2000)]
Unknowns = Annotated[list[Reason], Field(max_length=40)]
HypothesisStatus = Literal["supported", "refuted", "unknown"]
StopReason = Literal[
    "evidence_sufficient",
    "missing_input",
    "unsupported",
    "round_budget",
    "call_budget",
    "time_budget",
    "no_progress",
    "unknown_execution",
]


class Hypothesis(Contract):
    id: Name
    statement: Reason


class DiagnosticAnalysis(Contract):
    capability: Name
    inputs: dict[str, JsonValue]
    hypothesis_ids: Annotated[list[Name], Field(min_length=1, max_length=8)]

    _finite = field_validator("inputs")(finite_mapping)

    @field_validator("hypothesis_ids")
    @classmethod
    def unique_hypotheses(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Analysis hypothesis references must be unique")
        return value


class DiagnosisProposal(Contract):
    action: Literal["plan", "clarify"]
    title: Annotated[str, Field(max_length=200)] = ""
    rationale: Annotated[str, Field(max_length=2000)] = ""
    question: Annotated[str, Field(max_length=2000)] = ""
    hypotheses: Annotated[list[Hypothesis], Field(max_length=8)] = []
    allowed_capabilities: Annotated[list[Name], Field(max_length=16)] = []
    analyses: Annotated[list[DiagnosticAnalysis], Field(max_length=4)] = []
    budget: ResourceBudget = ResourceBudget()

    @model_validator(mode="after")
    def bounded_plan(self) -> Self:
        if self.action == "clarify":
            if not self.question.strip() or self.analyses or self.hypotheses:
                raise ValueError("A clarification needs a question and cannot execute analyses")
            return self
        if not all((self.title.strip(), self.rationale.strip(), self.hypotheses, self.analyses)):
            raise ValueError("A diagnosis plan needs a title, rationale, hypotheses and analyses")
        identifiers = [item.id for item in self.hypotheses]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Hypotheses must have unique identifiers")
        if len(self.allowed_capabilities) != len(set(self.allowed_capabilities)):
            raise ValueError("Allowed capabilities must be unique")
        for analysis in self.analyses:
            if analysis.capability not in self.allowed_capabilities:
                raise ValueError("An analysis must use a capability within the proposed scope")
            if not set(analysis.hypothesis_ids).issubset(identifiers):
                raise ValueError("An analysis refers to an unknown hypothesis")
        return self


class DiagnosisBaseline(Contract):
    version: Annotated[int, Field(strict=True, ge=1)]
    project_fingerprint: Digest
    design: DesignContext
    objective: Reason
    hypotheses: Annotated[list[Hypothesis], Field(min_length=1, max_length=8)]
    allowed_capabilities: Annotated[list[Name], Field(min_length=1, max_length=16)]
    capability_versions: dict[Name, Version]
    budget: ResourceBudget
    confirmed: Annotated[bool, Field(strict=True)] = False
    confirmed_at: str | None = None


class EvidenceCitation(Contract):
    run_id: RunId
    metric: Name | None = None


class HypothesisAssessment(Contract):
    hypothesis_id: Name
    status: HypothesisStatus
    reason: Reason
    evidence: Annotated[list[EvidenceCitation], Field(max_length=16)] = []

    @model_validator(mode="after")
    def grounded_assessment(self) -> Self:
        if self.status != "unknown" and not self.evidence:
            raise ValueError("Supported or refuted hypotheses require evidence citations")
        if len(self.evidence) != len({(item.run_id, item.metric) for item in self.evidence}):
            raise ValueError("Evidence citations must be unique")
        return self


class DiagnosisReview(Contract):
    action: Literal["continue", "stop"]
    reason: Reason
    assessments: Annotated[list[HypothesisAssessment], Field(min_length=1, max_length=8)]
    unknowns: Unknowns = []
    analyses: Annotated[list[DiagnosticAnalysis], Field(max_length=4)] = []
    stop_reason: Literal["evidence_sufficient", "missing_input", "unsupported"] | None = None

    @model_validator(mode="after")
    def explicit_decision(self) -> Self:
        ids = [item.hypothesis_id for item in self.assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("A review cannot assess the same hypothesis twice")
        if any(item.status == "unknown" for item in self.assessments) and not self.unknowns:
            raise ValueError("Unresolved hypotheses require explicit unknowns")
        if self.action == "continue":
            if not self.analyses or self.stop_reason is not None:
                raise ValueError("Continuing requires new analyses, not a stop reason")
        elif self.analyses or self.stop_reason is None:
            raise ValueError("Stopping requires a reason and cannot request more analyses")
        if self.stop_reason == "evidence_sufficient" and (
            any(item.status == "unknown" for item in self.assessments)
            or not any(item.status == "supported" for item in self.assessments)
        ):
            raise ValueError("Sufficient evidence requires resolved, supported hypotheses")
        return self


class DiagnosticRound(Contract):
    plan_version: Annotated[int, Field(strict=True, ge=1)]
    run_ids: Annotated[list[RunId], Field(max_length=4)]
    review: DiagnosisReview
    created_at: Nonempty


class DiagnosisPlanRevision(PlanRevision):
    analyses: Annotated[list[DiagnosticAnalysis], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def matching_specs(self) -> Self:
        if len(self.analyses) != len(self.analysis_specs) or any(
            analysis.capability != spec.capability or analysis.inputs != spec.inputs
            for analysis, spec in zip(self.analyses, self.analysis_specs, strict=True)
        ):
            raise ValueError("A plan revision must preserve its exact validated analysis inputs")
        return self


class PendingAnalysis(Contract):
    id: RunId
    spec: AnalysisSpec
    plan_version: Annotated[int, Field(strict=True, ge=1)]
    analysis_index: Annotated[int, Field(strict=True, ge=0, le=3)]
    started_at: Nonempty


class DiagnosisTermination(Contract):
    reason: StopReason
    detail: Reason
    created_at: Nonempty
    unknowns: Unknowns


def validate_assessments(
    hypotheses: list[Hypothesis],
    assessments: list[HypothesisAssessment],
    runs: list[AnalysisRun],
) -> None:
    identifiers = [item.hypothesis_id for item in assessments]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != {
        item.id for item in hypotheses
    }:
        raise ValueError("A diagnosis review must assess exactly the confirmed hypotheses")
    outputs = {run.id: DiagnosticOutput.model_validate(run.output) for run in runs}
    for assessment in assessments:
        for citation in assessment.evidence:
            if citation.run_id not in outputs:
                raise ValueError("Diagnosis cites unavailable evidence")
            output = outputs[citation.run_id]
            if citation.metric is not None and citation.metric not in output.metrics:
                raise ValueError("Diagnosis cites an unavailable evidence metric")
            if assessment.status != "unknown" and not output.valid:
                raise ValueError("Invalid evidence cannot support or refute a hypothesis")
