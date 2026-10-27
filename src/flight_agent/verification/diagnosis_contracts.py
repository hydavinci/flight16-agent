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
    priority: Literal["high", "medium", "low"] = "medium"


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


def unique_citations(value: list[EvidenceCitation]) -> list[EvidenceCitation]:
    if len(value) != len({(item.run_id, item.metric) for item in value}):
        raise ValueError("Evidence citations must be unique")
    return value


class HypothesisRevision(Contract):
    hypotheses: Annotated[list[Hypothesis], Field(min_length=1, max_length=8)]
    reason: Reason
    evidence: Annotated[list[EvidenceCitation], Field(min_length=1, max_length=16)]

    _unique_evidence = field_validator("evidence")(unique_citations)

    @field_validator("hypotheses")
    @classmethod
    def unique_hypotheses(cls, value: list[Hypothesis]) -> list[Hypothesis]:
        if len(value) != len({item.id for item in value}):
            raise ValueError("Hypotheses must have unique identifiers")
        return value


class DiagnosisSummary(Contract):
    problem: Reason
    localization: Reason
    responsibility: Literal["model", "control", "undetermined"]
    reason: Reason
    hypothesis_ids: Annotated[list[Name], Field(max_length=8)] = []
    evidence: Annotated[list[EvidenceCitation], Field(max_length=16)] = []
    recommendations: Annotated[list[Reason], Field(min_length=1, max_length=8)]

    _unique_evidence = field_validator("evidence")(unique_citations)

    @model_validator(mode="after")
    def grounded_responsibility(self) -> Self:
        if len(self.hypothesis_ids) != len(set(self.hypothesis_ids)):
            raise ValueError("Summary hypothesis references must be unique")
        if self.responsibility != "undetermined" and not (self.hypothesis_ids and self.evidence):
            raise ValueError("Responsibility attribution requires hypotheses and evidence")
        return self


class HypothesisAssessment(Contract):
    hypothesis_id: Name
    status: HypothesisStatus
    reason: Reason
    evidence: Annotated[list[EvidenceCitation], Field(max_length=16)] = []

    _unique_evidence = field_validator("evidence")(unique_citations)

    @model_validator(mode="after")
    def grounded_assessment(self) -> Self:
        if self.status != "unknown" and not self.evidence:
            raise ValueError("Supported or refuted hypotheses require evidence citations")
        return self


class DiagnosisReview(Contract):
    action: Literal["continue", "stop"]
    reason: Reason
    assessments: Annotated[list[HypothesisAssessment], Field(min_length=1, max_length=8)]
    unknowns: Unknowns = []
    analyses: Annotated[list[DiagnosticAnalysis], Field(max_length=4)] = []
    stop_reason: Literal["evidence_sufficient", "missing_input", "unsupported"] | None = None
    hypothesis_revision: HypothesisRevision | None = None
    summary: DiagnosisSummary | None = None

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
        raise ValueError("A diagnosis review must assess exactly the current hypotheses")
    outputs = {run.id: DiagnosticOutput.model_validate(run.output) for run in runs}
    for assessment in assessments:
        _validate_citations(
            assessment.evidence, outputs, require_valid=assessment.status != "unknown",
        )


def _validate_citations(
    citations: list[EvidenceCitation],
    outputs: dict[str, DiagnosticOutput],
    *,
    require_valid: bool,
) -> None:
    for citation in citations:
        if citation.run_id not in outputs:
            raise ValueError("Diagnosis cites unavailable evidence")
        output = outputs[citation.run_id]
        if citation.metric is not None and citation.metric not in output.metrics:
            raise ValueError("Diagnosis cites an unavailable evidence metric")
        if require_valid and not output.valid:
            raise ValueError("Invalid evidence cannot ground diagnosis conclusions or revisions")


def validate_review(
    hypotheses: list[Hypothesis],
    review: DiagnosisReview,
    runs: list[AnalysisRun],
) -> list[Hypothesis]:
    outputs = {run.id: DiagnosticOutput.model_validate(run.output) for run in runs}
    current = hypotheses
    if revision := review.hypothesis_revision:
        previous = {item.id: item for item in hypotheses}
        revised = {item.id: item for item in revision.hypotheses}
        if not previous.keys() <= revised.keys():
            raise ValueError("A revision cannot drop existing hypotheses")
        if previous == revised:
            raise ValueError("A hypothesis revision must record a change")
        _validate_citations(revision.evidence, outputs, require_valid=True)
        current = revision.hypotheses
    identifiers = {item.id for item in current}
    validate_assessments(current, review.assessments, runs)
    if any(not set(item.hypothesis_ids) <= identifiers for item in review.analyses):
        raise ValueError("An analysis refers to an unknown hypothesis")
    if summary := review.summary:
        if not set(summary.hypothesis_ids) <= identifiers:
            raise ValueError("Summary refers to an unknown hypothesis")
        _validate_citations(summary.evidence, outputs, require_valid=True)
        if summary.responsibility != "undetermined":
            supported = {
                item.hypothesis_id: item
                for item in review.assessments if item.status == "supported"
            }
            if not set(summary.hypothesis_ids) <= supported.keys():
                raise ValueError("Responsibility attribution requires supported hypotheses")
            cited = {
                (item.run_id, item.metric)
                for identifier in summary.hypothesis_ids
                for item in supported[identifier].evidence
            }
            if any((item.run_id, item.metric) not in cited for item in summary.evidence):
                raise ValueError("Responsibility evidence must link to supported hypotheses")
    return current


def validate_history(
    hypotheses: list[Hypothesis],
    plans: list[DiagnosisPlanRevision],
    rounds: list[DiagnosticRound],
    runs: list[AnalysisRun],
) -> list[Hypothesis]:
    if not plans or [plan.version for plan in plans] != list(range(1, len(plans) + 1)):
        raise ValueError("Diagnostic plan revisions must be contiguous and append-only")
    if (
        not 0 <= len(plans) - len(rounds) <= 1
        or [item.plan_version for item in rounds] != list(range(1, len(rounds) + 1))
    ):
        raise ValueError("Diagnostic rounds must follow their plans in order")
    recorded = {run.id: run for run in runs}
    if len(recorded) != len(runs):
        raise ValueError("Diagnostic evidence identifiers must be unique")
    available: dict[str, AnalysisRun] = {}
    current = hypotheses
    for index, plan in enumerate(plans):
        if not index:
            if plan.evidence:
                raise ValueError("The initial diagnostic plan cannot cite future evidence")
        else:
            previous = rounds[index - 1]
            if (
                previous.review.action != "continue"
                or plan.evidence != previous.run_ids
                or plan.reason != previous.review.reason
                or len(plan.analyses) != len(previous.review.analyses)
            ):
                raise ValueError("A diagnostic plan must follow its recorded evidence review")
            for analysis, proposed in zip(plan.analyses, previous.review.analyses, strict=True):
                if (
                    analysis.capability != proposed.capability
                    or analysis.hypothesis_ids != proposed.hypothesis_ids
                    or any(
                        key not in analysis.inputs or analysis.inputs[key] != value
                        for key, value in proposed.inputs.items()
                    )
                ):
                    raise ValueError("Diagnostic plan differs from its recorded next analyses")
        if any(
            not set(analysis.hypothesis_ids) <= {item.id for item in current}
            for analysis in plan.analyses
        ):
            raise ValueError("A diagnostic plan refers to an unavailable hypothesis")
        if index == len(rounds):
            break
        round_ = rounds[index]
        if len(round_.run_ids) != len(plan.analysis_specs):
            raise ValueError("A diagnostic round must record evidence for every planned analysis")
        for run_id, spec in zip(round_.run_ids, plan.analysis_specs, strict=True):
            run = recorded.get(run_id)
            if run is None or run.spec != spec:
                raise ValueError("Diagnostic round evidence does not match its plan")
            available[run.id] = run
        current = validate_review(current, round_.review, list(available.values()))
    return current
