import json
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter

from ..runtime.intents import Scenario
from .contracts import AgentTask, Conversation, DiagnosisTask, GeneralTask, Task, VerificationTask
from .resources import ResourceLocation

if TYPE_CHECKING:
    from .routes import MessageRequest
    from .runtime import VerificationRuntime
    from .views import TaskView

Action = Literal["confirm", "resume", "retry", "replan"]


def is_legacy_task(task: Task) -> bool:
    return not isinstance(task, AgentTask)


def has_unresolved_call(task: Task) -> bool:
    if isinstance(task, VerificationTask):
        request = task.pending_request
        if request is None:
            return False
        return not (
            task.worker_run is not None
            and task.worker_run.id == request.id
            and task.worker_run.status in ("failed", "interrupted")
        )
    if isinstance(task, DiagnosisTask):
        return task.pending_analysis is not None
    return any(call.status in ("running", "interrupted") for call in task.calls)


def resolve_location(
    location: str | None,
    scenario: str | None,
    locations: list[ResourceLocation],
) -> str | None:
    requested = {"d1": "simulink_project", "d2": "diagnostic_data"}.get(scenario or "")
    by_id = {item.id: item for item in locations}
    if location is not None:
        matches = [item.id for item in locations if item.id == location]
        legacy_alias = {"agent": "diagnostic_data", "worker": "simulink_project"}.get(location)
        if not matches and legacy_alias in by_id:
            matches = [legacy_alias]
        if not matches:
            matches = [item.id for item in locations if item.location == location]
        if len(matches) != 1:
            raise ValueError(
                "Resource location is unknown or ambiguous; select a registered location"
            )
        if requested is not None and requested != matches[0]:
            raise ValueError("Deprecated scenario conflicts with the selected resource location")
        return matches[0]
    if requested is not None:
        if requested not in by_id:
            raise ValueError("The deprecated scenario has no registered resource location")
        return requested
    return None


def translate_legacy_request(
    request: "MessageRequest",
    locations: list[ResourceLocation],
) -> "MessageRequest":
    return request.model_copy(
        update={
            "location": resolve_location(request.location, request.scenario, locations),
            "scenario": None,
        }
    )


def legacy_decision(
    runtime: "VerificationRuntime",
    conversation_id: str,
    action: Action,
    *,
    version: int | None = None,
    trusted: bool = False,
) -> Callable[[], Conversation] | None:
    record = runtime.store.get(conversation_id)
    task = record.task
    if task is None or isinstance(task, AgentTask):
        raise ValueError("No legacy task has been planned")
    runtime._check_active_task(record, task)
    if action != "resume" and (
        type(version) is not int or version != task.baseline.version or trusted is not True
    ):
        raise ValueError("Explicit consent for the current baseline version is required")
    if action in ("confirm", "resume") and record.status == "completed":
        return None
    if action == "confirm":
        if task.baseline.confirmed:
            raise ValueError("This baseline is already confirmed; use resume")
        return partial(runtime.confirm, conversation_id, task.baseline.version, trusted=trusted)
    if action == "resume":
        if not task.baseline.confirmed:
            raise ValueError("Confirm the current baseline before continuing")
        return partial(runtime.resume, conversation_id)
    if action == "replan":
        if (
            not isinstance(task, VerificationTask)
            or record.pending_decision is None
            or record.pending_decision.kind != "confirm_replan"
            or task.failure_proposal is None
            or task.failure_proposal.action != "plan"
        ):
            raise ValueError("No recovery plan is awaiting explicit adoption")
        if has_unresolved_call(task):
            raise ValueError("Reconcile the unknown Worker run before adopting another plan")
        return partial(runtime.replan, conversation_id, task.baseline.version, trusted=trusted)
    if (
        not isinstance(task, VerificationTask)
        or not task.baseline.confirmed
        or task.pending_request is None
        or task.worker_run is None
        or not runtime._known_failed_run(task)
    ):
        raise ValueError("Only a known failed or interrupted worker run may be retried")
    return partial(runtime.retry, conversation_id, task.baseline.version, trusted=trusted)


class LegacyRuntimeAdapter:
    """Explicit legacy injection preserves old-client tests and saved task actions."""

    def __init__(self, runtime: "VerificationRuntime"):
        self.runtime = runtime
        self.store = runtime.store

    def queue_message(
        self,
        conversation_id: str | None,
        message: str,
        project_root: str | None = None,
        *,
        output_root: str | None = None,
        location: str | None = None,
        scenario: str | None = None,
    ) -> Conversation:
        if location is not None:
            raise ValueError("Registered resource locations require the generic entry")
        selected: Scenario | None = TypeAdapter(Scenario | None).validate_python(scenario)
        return self.runtime.queue_message(
            conversation_id,
            message,
            project_root,
            output_root=output_root,
            scenario=selected,
        )

    def continue_submission(self, conversation_id: str) -> Conversation:
        return self.runtime.continue_submission(conversation_id)

    def prepare_decision(
        self,
        conversation_id: str,
        action: Action,
        *,
        version: int | None = None,
        trusted: bool = False,
    ) -> Callable[[], Conversation] | None:
        return legacy_decision(
            self.runtime,
            conversation_id,
            action,
            version=version,
            trusted=trusted,
        )

    def capability_catalog(self) -> list[dict[str, object]]:
        return self.runtime.workflows.catalog()

    def workflow_catalog(self) -> list[dict[str, object]]:
        return self.runtime.workflows.catalog()


def to_legacy_view(task: Task) -> "TaskView":
    from ..runtime.observations import Finding, Observation, ResultTable
    from ..runtime.resources import ResourceReference
    from .views import (
        CheckView,
        ConfirmationView,
        CriterionView,
        EvaluationView,
        MetricView,
        NodeView,
        RequirementView,
        TaskView,
    )

    if isinstance(task, AgentTask):
        raise ValueError("A v2 task must use its own view projection")
    requirements = RequirementView(
        objective=task.baseline.objective
        if not isinstance(task, VerificationTask)
        else task.proposal.rationale,
        mode="verification" if isinstance(task, VerificationTask) else "analysis",
    )
    nodes = [
        NodeView(
            id=node.id,
            tool=node.tool,
            depends_on=node.depends_on,
            inputs=node.inputs,
            status=node.status,
            error=node.error,
            tool_version=(
                task.baseline.specs[node.id].capability_version
                if isinstance(task, GeneralTask) and node.id in task.baseline.specs
                else next(
                    (call.tool_version for call in reversed(task.calls) if call.node_id == node.id),
                    None,
                )
            ),
        )
        for node in task.nodes
    ]
    view = TaskView(
        id=task.id,
        schema_version="1.0",
        title=task.proposal.title,
        baseline_version=task.baseline.version,
        confirmation=ConfirmationView(
            confirmed=task.baseline.confirmed,
            confirmed_at=task.baseline.confirmed_at,
            recovery_action=(
                "retry"
                if task.pending_request is not None and not has_unresolved_call(task)
                else "resume"
            )
            if isinstance(task, VerificationTask)
            and task.failure_proposal is not None
            and task.failure_proposal.action == "plan"
            else None,
            recovery_proposal=(
                task.failure_proposal.model_dump(mode="json")
                if isinstance(task, VerificationTask)
                and task.failure_proposal is not None
                and task.failure_proposal.action == "plan"
                else None
            ),
        ),
        resources=[],
        requirements=requirements,
        permissions=(
            list(task.baseline.allowed_effects)
            if isinstance(task, GeneralTask)
            else ["execute_project", "write_delivery"]
            if isinstance(task, VerificationTask)
            else ["read_only", "write_delivery"]
        ),
        budget=None if isinstance(task, VerificationTask) else task.baseline.budget,
        nodes=nodes,
        artifacts=list(task.artifacts),
        stage=task.stage,
        result_directory=task.result_directory,
        calls=task.calls,
        error="\n".join(node.error for node in task.nodes if node.error) or None,
        details={"historical_record": task.model_dump(mode="json")},
    )
    if isinstance(task, GeneralTask):
        return view.model_copy(
            update={
                "findings": [
                    Finding(id=node, text=json.dumps(output, ensure_ascii=False, indent=2))
                    for node, output in task.outputs.items()
                ]
            }
        )
    view = view.model_copy(
        update={
            "resources": [
                ResourceReference(
                    id="historical-project",
                    kind="simulink_project"
                    if isinstance(task, VerificationTask)
                    else "diagnostic_data",
                    location="worker" if isinstance(task, VerificationTask) else "agent",
                    locator=task.project.root,
                    fingerprint=task.project.fingerprint,
                )
            ]
        }
    )
    if isinstance(task, VerificationTask):
        requirements = requirements.model_copy(
            update={
                "overall_requested": True,
                "criteria": [
                    CriterionView(
                        id=f"historical-{index}",
                        metric=MetricView(metric_id=criterion.metric),
                        operator=criterion.operator,
                        threshold=criterion.threshold,
                        unit=criterion.unit,
                        conditions={"trim_point": task.baseline.trim_point},
                        source_note="历史记录未提供来源",
                    )
                    for index, criterion in enumerate(task.baseline.criteria)
                ],
            }
        )
        view = view.model_copy(update={"requirements": requirements})
        if task.result is not None:
            observations = []
            for analysis in task.result.analyses:
                analysis_valid = analysis.valid and analysis.execution_status == "completed"
                for name, value in analysis.metrics.items():
                    reason = value.reason
                    if not analysis_valid:
                        reason = "\n".join(
                            filter(
                                None,
                                [
                                    reason,
                                    *analysis.diagnostics,
                                    f"Historical analysis {analysis.kind}: "
                                    f"execution={analysis.execution_status}, "
                                    f"valid={analysis.valid}.",
                                ],
                            )
                        )
                    if len(reason) > 6000:
                        reason = (
                            reason[:5900] + "\n[Truncated for display; full reasons in raw record.]"
                        )
                    observations.append(
                        Observation(
                            **{
                                **value.model_dump(),
                                "valid": value.valid and analysis_valid,
                                "reason": reason,
                            },
                            node_id=analysis.kind,
                            metric_id=name,
                            label=name,
                            conditions={"trim_point": task.result.trim_point},
                            evidence=[task.result.run_id],
                        )
                    )
            view = view.model_copy(update={"observations": observations})
        if task.evaluation is not None:
            by_metric = {item.metric.metric_id: item.id for item in requirements.criteria}
            view = view.model_copy(
                update={
                    "evaluation": EvaluationView(
                        status=task.evaluation.status,
                        checks=[
                            CheckView(
                                criterion_id=by_metric[check.metric],
                                metric=MetricView(metric_id=check.metric),
                                value=check.value,
                                special=check.special,
                                value_unit=check.value_unit,
                                operator=check.operator,
                                threshold=check.threshold,
                                unit=check.unit,
                                status=check.status,
                                reason=check.reason,
                                evidence=check.evidence,
                            )
                            for check in task.evaluation.checks
                        ],
                        warnings=task.evaluation.warnings,
                    )
                }
            )
        if task.explanation is not None:
            view.findings.append(Finding(id="explanation", text=task.explanation.text))
        if task.explanation_error:
            view.findings.append(
                Finding(
                    id="explanation-error",
                    text=task.explanation_error,
                    status="incomplete",
                )
            )
        return view
    summary = task.rounds[-1].review.summary if task.rounds else None
    from .diagnosis_contracts import EvidenceCitation

    def evidence(refs: list[EvidenceCitation]) -> list[str]:
        return [ref.run_id + (f" / {ref.metric}" if ref.metric is not None else "") for ref in refs]

    if summary is not None:
        responsibility = {"model": "模型侧", "control": "控制侧", "undetermined": "尚无法归属"}
        view.findings.append(
            Finding(
                id="diagnosis-summary",
                text="\n".join(
                    [
                        f"问题：{summary.problem}",
                        f"定位：{summary.localization}",
                        f"责任归属：{responsibility[summary.responsibility]}",
                        f"判断依据：{summary.reason}",
                        "后续建议（仅供复核，不自动执行）：",
                        *summary.recommendations,
                    ]
                ),
                evidence=evidence(summary.evidence),
            )
        )
    else:
        view.findings.append(
            Finding(
                id="diagnosis-summary",
                text="结构化诊断未记录；不补推历史结论。"
                if task.rounds
                else "尚无结构化结论，等待证据评审。",
                status="unknown",
            )
        )
    hypotheses = task.baseline.hypotheses
    revisions: list[list[object]] = []
    for round_ in task.rounds:
        revision = round_.review.hypothesis_revision
        if revision is not None:
            before = {item.id: item for item in hypotheses}
            for item in revision.hypotheses:
                revisions.append(
                    [
                        round_.plan_version,
                        item.id,
                        "新增" if item.id not in before else "修订",
                        item.statement,
                        revision.reason,
                        "\n".join(evidence(revision.evidence)),
                    ]
                )
            hypotheses = revision.hypotheses
    assessments = {item.hypothesis_id: item for item in task.assessments}
    priorities = {"high": "高", "medium": "中", "low": "低"}
    labels = {"supported": "支持", "refuted": "反驳", "unknown": "未知"}
    view.tables.append(
        ResultTable(
            id="hypothesis-assessments",
            columns=["假设 / 陈述", "优先级（分析顺序，非置信度）", "证据状态", "依据", "引用"],
            rows=[
                [
                    f"{item.id}\n{item.statement}",
                    priorities[item.priority],
                    labels[assessments[item.id].status] if item.id in assessments else "待评审",
                    assessments[item.id].reason if item.id in assessments else "尚未记录评审结论。",
                    "\n".join(evidence(assessments[item.id].evidence))
                    if item.id in assessments
                    else "",
                ]
                for item in hypotheses
            ],
        )
    )
    if revisions:
        view.tables.append(
            ResultTable.model_validate(
                {
                    "id": "hypothesis-history",
                    "columns": ["计划", "假设", "变化", "陈述", "依据", "引用"],
                    "rows": revisions,
                }
            )
        )
    if task.termination is not None:
        labels = {
            "missing_input": "缺少必要输入",
            "evidence_sufficient": "已有证据足以评审当前假设",
            "unsupported": "超出已支持范围",
            "round_budget": "已达轮次上限",
            "call_budget": "已达调用上限",
            "time_budget": "已达时间上限",
            "no_progress": "连续评审未获得新证据",
            "unknown_execution": "执行状态无法确认",
        }
        view.findings.append(
            Finding(
                id="termination",
                text="\n".join(
                    [
                        f"停止原因：{labels[task.termination.reason]}",
                        task.termination.detail,
                        *[f"未知 / 限制：{item}" for item in task.termination.unknowns],
                        "支持或反驳不证明唯一物理根因或工程验收。",
                    ]
                ),
            )
        )
    return view
