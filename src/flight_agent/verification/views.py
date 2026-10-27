from typing import Literal

from pydantic import JsonValue

from ..models import Contract, FiniteNumber
from ..runtime.agent_contracts import BudgetState
from ..runtime.contracts import Effect, ResourceBudget
from ..runtime.observations import (
    Finding,
    Observation,
    OutputArtifact,
    ResultTable,
    combine_outcomes,
)
from ..runtime.requirements import CapabilityGap, CriterionSource, OutputRequirement
from ..runtime.resources import ResourceReference
from .contracts import (
    AgentTask,
    Artifact,
    Conversation,
    ConversationMessage,
    ConversationStatus,
    PendingDecision,
    Task,
    ToolCall,
)


class MetricView(Contract):
    node_id: str | None = None
    metric_id: str


class CriterionView(Contract):
    id: str
    metric: MetricView
    operator: Literal["<", "<=", "==", ">=", ">"]
    threshold: FiniteNumber
    unit: str
    conditions: dict[str, JsonValue] = {}
    source: CriterionSource | None = None
    source_note: str = ""


class RequirementView(Contract):
    objective: str
    mode: Literal["analysis", "verification"]
    requested_outputs: list[OutputRequirement] = []
    criteria: list[CriterionView] = []
    overall_requested: bool = False
    delivery_formats: list[str] = []


class ConfirmationView(Contract):
    confirmed: bool
    confirmed_at: str | None = None
    recovery_action: Literal["resume", "retry"] | None = None
    recovery_proposal: dict[str, JsonValue] | None = None


class NodeView(Contract):
    id: str
    tool: str
    tool_version: str | None = None
    depends_on: list[str] = []
    inputs: dict[str, JsonValue] = {}
    bindings: dict[str, JsonValue] = {}
    resource_bindings: dict[str, str] = {}
    status: str
    error: str | None = None


class CheckView(Contract):
    criterion_id: str
    metric: MetricView
    value: FiniteNumber | None = None
    special: str | None = None
    value_unit: str | None = None
    operator: str
    threshold: FiniteNumber
    unit: str
    status: Literal["pass", "fail", "undetermined"]
    reason: str
    evidence: list[str] = []


class EvaluationView(Contract):
    status: Literal["pass", "fail", "undetermined"] | None
    checks: list[CheckView]
    warnings: list[str] = []


class TaskView(Contract):
    """Read-only display, including archived logs larger than execution payload strings."""
    id: str
    schema_version: Literal["1.0", "2.0"]
    title: str
    baseline_version: int
    confirmation: ConfirmationView
    resources: list[ResourceReference]
    requirements: RequirementView
    permissions: list[Effect]
    budget: ResourceBudget | None
    budget_state: BudgetState | None = None
    nodes: list[NodeView]
    observations: list[Observation] = []
    findings: list[Finding] = []
    tables: list[ResultTable] = []
    evaluation: EvaluationView | None = None
    gaps: list[CapabilityGap] = []
    artifacts: list[OutputArtifact | Artifact] = []
    error: str | None = None
    stage: str
    execution_status: str | None = None
    result_directory: str
    calls: list[ToolCall] = []
    details: dict[str, JsonValue] = {}


class ConversationView(Contract):
    id: str
    status: ConversationStatus
    project_root: str | None
    resource_location: str | None
    output_root: str | None
    task: TaskView | None
    archived_tasks: list[TaskView]
    messages: list[ConversationMessage]
    pending_decision: PendingDecision | None
    error: str | None


def to_task_view(task: AgentTask) -> TaskView:
    baseline = task.baseline
    outcome = task.result if task.result is not None else combine_outcomes(task.outputs)
    return TaskView(
        id=task.id,
        schema_version="2.0",
        title=baseline.title,
        baseline_version=baseline.version,
        confirmation=ConfirmationView(
            confirmed=baseline.confirmed,
            confirmed_at=baseline.confirmed_at,
        ),
        resources=baseline.resources,
        requirements=RequirementView.model_validate(baseline.requirements.model_dump()),
        permissions=list(baseline.allowed_effects),
        budget=baseline.budget,
        budget_state=task.budget_state,
        nodes=[NodeView.model_validate(node.model_dump()) for node in task.nodes],
        observations=outcome.observations,
        findings=outcome.findings,
        tables=outcome.tables,
        evaluation=(
            EvaluationView.model_validate(outcome.evaluation.model_dump())
            if outcome.evaluation is not None
            else None
        ),
        gaps=baseline.gaps + (task.result.unmet_requirements if task.result is not None else []),
        artifacts=list(task.artifacts),
        stage=task.stage,
        result_directory=task.result_directory,
        execution_status=task.result.execution_status if task.result is not None else None,
        error="\n".join(node.error for node in task.nodes if node.error) or None,
        calls=task.calls,
        details={
            "capability_snapshots": {
                name: snapshot.model_dump(mode="json")
                for name, snapshot in baseline.capability_snapshots.items()
            },
            "outcomes": {
                name: value.model_dump(mode="json") for name, value in task.outputs.items()
            },
            "reuse_sources": dict(task.reuse_sources),
        },
    )


def to_conversation_view(record: Conversation) -> ConversationView:
    from .compat import to_legacy_view

    def project(task: Task) -> TaskView:
        return to_task_view(task) if isinstance(task, AgentTask) else to_legacy_view(task)

    return ConversationView(
        id=record.id,
        status=record.status,
        project_root=record.project_root,
        resource_location=record.resource_location,
        output_root=record.output_root,
        task=project(record.task) if record.task is not None else None,
        archived_tasks=[project(task) for task in record.archived_tasks],
        messages=record.messages,
        pending_decision=record.pending_decision,
        error=record.error,
    )
