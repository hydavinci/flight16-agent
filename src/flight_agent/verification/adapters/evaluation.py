import math
import operator
from collections.abc import Callable
from typing import Annotated, Protocol

from pydantic import Field, StrictBool

from ...runtime.agent_contracts import AgentNode, AgentPlan
from ...runtime.capabilities import Capability
from ...runtime.capability_metadata import CapabilityMetadata
from ...runtime.observations import (
    CapabilityOutcome,
    CriterionCheck,
    EvaluationResult,
    EvaluationStatus,
    Observation,
)
from ...runtime.requirements import Criterion
from ...runtime.resources import AgentContract

COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    ">=": operator.ge,
    ">": operator.gt,
}


class OutcomeContext(Protocol):
    @property
    def outputs(self) -> dict[str, CapabilityOutcome]: ...


class ScalarEvaluationInput(AgentContract):
    criteria: Annotated[list[Criterion], Field(min_length=1, max_length=80)]
    observations: Annotated[list[Observation], Field(max_length=8000)] = []
    overall_requested: StrictBool = False


def evaluate_criteria(
    criteria: list[Criterion], observations: list[Observation], *, overall_requested: bool
) -> EvaluationResult:
    inputs = ScalarEvaluationInput(
        criteria=criteria, observations=observations, overall_requested=overall_requested
    )
    checks = []
    for criterion in inputs.criteria:
        matches = [
            observation
            for observation in inputs.observations
            if observation.node_id == criterion.metric.node_id
            and observation.metric_id == criterion.metric.metric_id
            and observation.conditions == criterion.conditions
        ]
        observation = matches[0] if len(matches) == 1 else None
        status: EvaluationStatus = "undetermined"
        if not matches:
            reason = "Required evidence is missing for the specified producer and conditions."
        elif len(matches) > 1:
            reason = "Required evidence is ambiguous for the specified producer and conditions."
        elif observation is None:
            raise ValueError("A unique observation was not selected")
        elif observation.unit != criterion.unit:
            reason = "Observation unit differs; a registered conversion is required."
        elif not observation.valid:
            reason = observation.reason
        else:
            value = observation.value
            if value is None:
                value = (
                    math.inf
                    if observation.special == "positive_infinity"
                    else -math.inf
                    if observation.special == "negative_infinity"
                    else None
                )
            if value is None:
                reason = "Producer supplied no comparable value."
            else:
                status = (
                    "pass"
                    if COMPARATORS[criterion.operator](value, criterion.threshold)
                    else "fail"
                )
                reason = (
                    "The value meets the criterion."
                    if status == "pass"
                    else ("The value does not meet the criterion.")
                )
        checks.append(
            CriterionCheck(
                criterion_id=criterion.id,
                metric=criterion.metric,
                value=observation.value if observation is not None else None,
                special=observation.special if observation is not None else None,
                value_unit=observation.unit if observation is not None else None,
                operator=criterion.operator,
                threshold=criterion.threshold,
                unit=criterion.unit,
                status=status,
                reason=reason,
                evidence=observation.evidence if observation is not None else [],
            )
        )
    statuses = [check.status for check in checks]
    overall: EvaluationStatus | None = None
    if inputs.overall_requested:
        overall = (
            "undetermined"
            if "undetermined" in statuses
            else "fail"
            if "fail" in statuses
            else "pass"
        )
    return EvaluationResult(status=overall, checks=checks)


def _validate_plan(plan: AgentPlan, node: AgentNode) -> None:
    if plan.requirements.mode != "verification" or plan.evaluation_node != node.id:
        raise ValueError("Scalar evaluation requires the confirmed verification evaluation node")
    inputs = ScalarEvaluationInput.model_validate(node.inputs)
    if inputs.criteria != plan.requirements.criteria or (
        inputs.overall_requested != plan.requirements.overall_requested
    ):
        raise ValueError("Evaluation inputs differ from the confirmed criteria")
    if not {item.metric.node_id for item in plan.requirements.criteria} <= set(node.depends_on):
        raise ValueError("Evaluation requires directly declared evidence producers")
    if node.inputs.get("observations"):
        raise ValueError("Evaluation evidence must come from actual dependency outputs")
    for field, reference in node.bindings.items():
        if field != "observations" or reference.path != ["observations"]:
            raise ValueError("Evaluation bindings may only reference actual observation arrays")


def _evaluate(context: OutcomeContext, inputs: ScalarEvaluationInput) -> CapabilityOutcome:
    outputs = {
        name: CapabilityOutcome.model_validate(output) for name, output in context.outputs.items()
    }
    if inputs.observations and not any(
        inputs.observations == output.observations for output in outputs.values()
    ):
        raise ValueError("Bound evaluation evidence differs from its dependency output")
    return CapabilityOutcome(
        evaluation=evaluate_criteria(
            inputs.criteria,
            [observation for output in outputs.values() for observation in output.observations],
            overall_requested=inputs.overall_requested,
        )
    )


def scalar_evaluation_capability() -> Capability[
    OutcomeContext, ScalarEvaluationInput, CapabilityOutcome
]:
    return Capability(
        name="evaluate_criteria",
        version="1",
        description="Compare explicitly sourced scalar criteria using exact units and conditions.",
        input_model=ScalarEvaluationInput,
        output_model=CapabilityOutcome,
        effect="read_only",
        operation=_evaluate,
        validate_plan=_validate_plan,
        metadata=CapabilityMetadata(
            validity_policy={
                "version": "1",
                "comparison": "exact-without-implicit-tolerance",
                "aggregation": "missing-or-invalid-then-fail-then-pass",
                "specials": "Only producer-declared valid infinities are comparable.",
            },
            planning_notes=(
                "Confirm exact criteria and overall_requested. Declare every criterion producer "
                "as a direct dependency. Observations are combined from those verified outputs; "
                "an optional observations binding must reference a genuine dependency array."
            ),
            cacheable=True,
        ),
    )
