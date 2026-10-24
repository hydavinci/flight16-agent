from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from ..models import Contract, FiniteNumber, Nonempty
from .contracts import EvidenceFile, Name
from .requirements import CapabilityGap, MetricReference, OutputRequirement, TaskRequirements
from .resources import AgentContract, Text

SpecialValue = Literal["positive_infinity", "negative_infinity", "undefined"]
EvaluationStatus = Literal["pass", "fail", "undetermined"]


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


class Observation(MetricValue, AgentContract):
    node_id: Name
    metric_id: Name
    label: Text
    conditions: dict[str, JsonValue] = {}
    evidence: Annotated[list[Text], Field(max_length=80)] = []


class Finding(AgentContract):
    id: Name
    node_id: Name | None = None
    text: Text
    status: Annotated[str, Field(min_length=1, max_length=100)] = "observed"
    evidence: Annotated[list[Text], Field(max_length=80)] = []


class ResultTable(AgentContract):
    id: Name
    node_id: Name | None = None
    columns: Annotated[list[Text], Field(min_length=1, max_length=100)]
    rows: Annotated[list[list[JsonValue]], Field(max_length=10000)] = []
    evidence: Annotated[list[Text], Field(max_length=80)] = []

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("Table columns must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Table rows must match their columns")
        return self


class CriterionCheck(AgentContract):
    criterion_id: Name
    metric: MetricReference
    value: FiniteNumber | None = None
    special: SpecialValue | None = None
    value_unit: str | None = None
    operator: Literal["<", "<=", "==", ">=", ">"]
    threshold: FiniteNumber
    unit: Text
    status: EvaluationStatus
    reason: Text
    evidence: Annotated[list[Text], Field(max_length=80)] = []

    @model_validator(mode="after")
    def decisive_value(self) -> Self:
        if self.value is not None and self.special is not None:
            raise ValueError("A check cannot contain both a value and a special value")
        if self.status != "undetermined" and (
            self.value_unit != self.unit
            or (
                self.value is None
                and self.special not in ("positive_infinity", "negative_infinity")
            )
        ):
            raise ValueError("Decisive checks require a comparable value in the criterion unit")
        return self


class EvaluationResult(AgentContract):
    status: EvaluationStatus | None = None
    checks: Annotated[list[CriterionCheck], Field(max_length=80)]

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if len({item.criterion_id for item in self.checks}) != len(self.checks):
            raise ValueError("Evaluation check IDs must be unique")
        if self.status is not None:
            statuses = [check.status for check in self.checks]
            expected = (
                "undetermined"
                if "undetermined" in statuses
                else "fail"
                if "fail" in statuses
                else "pass"
            )
            if not statuses or self.status != expected:
                raise ValueError("Evaluation status is inconsistent with its checks")
        return self

    def validate_requirements(self, requirements: TaskRequirements, *, complete: bool) -> None:
        checks = {item.criterion_id: item for item in self.checks}
        criterion_ids = {item.id for item in requirements.criteria}
        if not set(checks) <= criterion_ids or (complete and set(checks) != criterion_ids):
            raise ValueError("Evaluation must cover exactly the requested criteria")
        for criterion in requirements.criteria:
            if criterion.id not in checks:
                continue
            check = checks[criterion.id]
            if (
                check.metric != criterion.metric
                or check.threshold != criterion.threshold
                or check.operator != criterion.operator
                or check.unit != criterion.unit
            ):
                raise ValueError("Evaluation differs from the confirmed criterion")
        if requirements.overall_requested != (self.status is not None):
            raise ValueError("Overall evaluation does not match the request")


class OutputArtifact(EvidenceFile, AgentContract):
    id: Name | None = None
    node_id: Name | None = None

    @model_validator(mode="before")
    @classmethod
    def revalidate_file(cls, value: object) -> object:
        if isinstance(value, EvidenceFile):
            return value.model_dump(mode="json")
        return value


class CapabilityOutcome(AgentContract):
    observations: Annotated[list[Observation], Field(max_length=200)] = []
    findings: Annotated[list[Finding], Field(max_length=200)] = []
    tables: Annotated[list[ResultTable], Field(max_length=40)] = []
    evaluation: EvaluationResult | None = None
    artifacts: Annotated[list[OutputArtifact], Field(max_length=200)] = []
    details: dict[str, JsonValue] = {}

    def missing_outputs(self, requirements: TaskRequirements) -> list[OutputRequirement]:
        missing = []
        for output in requirements.requested_outputs:
            if output.node_id is None or output.item_id is None:
                missing.append(output)
                continue
            if output.kind == "metric":
                exists = any(
                    item.node_id == output.node_id and item.metric_id == output.item_id
                    for item in self.observations
                )
            else:
                items = (
                    self.findings
                    if output.kind == "finding"
                    else self.tables
                    if output.kind == "table"
                    else self.artifacts
                )
                exists = any(
                    item.id == output.item_id and item.node_id == output.node_id for item in items
                )
            if not exists:
                missing.append(output)
        return missing


def combine_outcomes(outputs: dict[str, CapabilityOutcome]) -> CapabilityOutcome:
    evaluations = [
        output.evaluation for output in outputs.values() if output.evaluation is not None
    ]
    if len(evaluations) > 1:
        raise ValueError("Results require one unambiguous evaluation")
    return CapabilityOutcome(
        observations=[item for output in outputs.values() for item in output.observations],
        findings=[item for output in outputs.values() for item in output.findings],
        tables=[item for output in outputs.values() for item in output.tables],
        evaluation=evaluations[0] if evaluations else None,
        artifacts=[item for output in outputs.values() for item in output.artifacts],
        details={
            "dependencies": {
                name: output.details for name, output in outputs.items() if output.details
            }
        },
    )


class TaskResult(CapabilityOutcome):
    execution_status: Literal["completed", "partial", "failed", "interrupted"]
    unmet_requirements: Annotated[list[CapabilityGap], Field(max_length=80)] = []

    @model_validator(mode="after")
    def disclosed_partial(self) -> Self:
        if self.execution_status == "completed" and self.unmet_requirements:
            raise ValueError("A result with unmet requirements must be partial")
        return self

    def validate_requirements(self, requirements: TaskRequirements) -> None:
        current = type(self).model_validate(self)
        requirements = TaskRequirements.model_validate(requirements)
        missing = {gap.requirement_id for gap in current.unmet_requirements}
        for output in current.missing_outputs(requirements):
            if output.id not in missing:
                raise ValueError(
                    f"Missing output without an explicit unmet requirement: {output.id}"
                )
        if requirements.mode == "analysis":
            if current.evaluation is not None:
                raise ValueError("Analysis did not request evaluation")
            return
        if current.evaluation is None:
            if current.execution_status == "completed":
                raise ValueError("Completed verification requires evaluation")
            return
        current.evaluation.validate_requirements(
            requirements, complete=current.execution_status == "completed"
        )
