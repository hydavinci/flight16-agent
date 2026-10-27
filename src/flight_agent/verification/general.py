import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from ..runtime.capabilities import CapabilityRegistry
from ..runtime.contracts import AnalysisRun, AnalysisSpec, ExecutionScope, fingerprint
from ..runtime.evidence import load_run, persist_run, receipt_name
from ..runtime.loop import LoopDecision, run_loop
from ..runtime.tasks import GeneralBaseline, GeneralProposal, PlanNode
from ..session import _write_json
from .artifacts import describe_artifact, verify_artifact
from .contracts import (
    Conversation,
    ConversationMessage,
    Decision,
    ExecutionNode,
    GeneralTask,
    PendingDecision,
    ToolCall,
)
from .intake import validate_output_root
from .store import ConversationStore, now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeneralContext:
    directory: Path
    scope: ExecutionScope
    outputs: dict[str, dict[str, JsonValue]]


class GeneralRuntime:
    def __init__(
        self, store: ConversationStore, tools: CapabilityRegistry[GeneralContext] | None = None
    ):
        self.store = store
        self.tools = tools if tools is not None else CapabilityRegistry()

    def _scope(self, task: GeneralTask) -> ExecutionScope:
        return ExecutionScope(
            baseline_version=task.baseline.version, confirmed=task.baseline.confirmed,
            design=task.baseline.design, allowed_effects=task.baseline.allowed_effects,
        )

    def _spec(self, proposal: GeneralProposal, node: PlanNode) -> AnalysisSpec:
        definition = self.tools.definition(node.tool)
        if definition.effect not in proposal.allowed_effects:
            raise ValueError("Capability effect is outside the proposed permissions")
        return AnalysisSpec(
            capability=node.tool, capability_version=definition.version,
            design=proposal.design, inputs=self.tools.validate_inputs(node.tool, node.inputs),
        )

    def propose(
        self, conversation: Conversation, proposal: GeneralProposal, *, workflow: str
    ) -> Conversation:
        proposal = GeneralProposal.model_validate(proposal.model_dump())
        specs = {node.id: self._spec(proposal, node) for node in proposal.nodes}
        effective = conversation.output_root or str(self.store.default_task_root(conversation.id))
        resolved = validate_output_root(effective)
        output_root = resolved if conversation.output_root is not None else None
        prior = [*conversation.archived_tasks, *([conversation.task] if conversation.task else [])]
        baseline = GeneralBaseline(
            version=max((task.baseline.version for task in prior), default=0) + 1,
            objective=proposal.title, design=proposal.design, specs=specs,
            allowed_effects=proposal.allowed_effects, budget=proposal.budget,
        )
        task_id = uuid4().hex
        task = GeneralTask(
            id=task_id, workflow=workflow, baseline=baseline, proposal=proposal,
            result_directory=str(
                self.store.task_directory(conversation.id, task_id, output_root, create=True)
            ),
            output_root=output_root,
            nodes=[ExecutionNode(**node.model_dump()) for node in proposal.nodes],
        )
        if conversation.task is not None:
            conversation.archived_tasks.append(conversation.task)
        conversation.task = task
        conversation.project_root = None
        conversation.output_root = output_root
        conversation.turn_origin = None
        conversation.status = "waiting_for_user"
        conversation.error = None
        conversation.pending_decision = PendingDecision(
            kind="confirm_baseline",
            message=f"请核对基线 v{baseline.version} 的输入、设计版本、能力、权限与预算后确认。"
            f" 成果目录：{task.result_directory}",
        )
        self._message(conversation, conversation.pending_decision.message)
        self.store.save(conversation)
        return conversation

    def _validate_pending(self, task: GeneralTask) -> None:
        if (
            task.baseline.design != task.proposal.design
            or task.baseline.allowed_effects != task.proposal.allowed_effects
            or task.baseline.budget != task.proposal.budget
        ):
            raise ValueError("Task scope differs from the proposed baseline")
        expected = {node.id: node for node in task.proposal.nodes}
        if len(task.nodes) != len(expected) or set(task.baseline.specs) != set(expected):
            raise ValueError("Task nodes differ from the proposed baseline")
        seen = set()
        for node in task.nodes:
            if node.id in seen or node.id not in expected:
                raise ValueError("Task nodes differ from the proposed baseline")
            seen.add(node.id)
            original = expected[node.id]
            spec = task.baseline.specs[node.id]
            if (
                node.tool != original.tool or node.depends_on != original.depends_on
                or node.inputs != original.inputs or spec.capability != original.tool
                or spec.design != task.baseline.design
            ):
                raise ValueError("Task inputs differ from the proposed baseline")
            if node.status == "completed":
                continue
            unresolved = any(
                call.node_id == node.id and call.status in ("running", "interrupted")
                for call in task.calls
            )
            if unresolved:
                continue
            if (
                self.tools.definition(node.tool).version != spec.capability_version
                or self._spec(task.proposal, original) != spec
            ):
                raise ValueError(
                    "Pending capability version or inputs changed; a new plan is required"
                )

    def confirm(self, conversation: Conversation, task: GeneralTask) -> Conversation:
        try:
            self._validate_pending(task)
        except ValueError as exc:
            conversation.status = "waiting_for_user"
            conversation.error = str(exc)[:4000]
            conversation.pending_decision = PendingDecision(
                kind="clarify", message="确认前能力或输入已变化，请重新规划：" + str(exc)[:2000]
            )
            self._message(conversation, conversation.pending_decision.message)
            self.store.save(conversation)
            return conversation
        task.baseline = GeneralBaseline.model_validate(
            {**task.baseline.model_dump(), "confirmed": True, "confirmed_at": now()}
        )
        task.decisions.append(
            Decision(
                action="confirm_baseline", baseline_version=task.baseline.version,
                created_at=now(), trusted=True,
            )
        )
        self.store.save(conversation)
        return self.run(conversation, task)

    def _directory(self, conversation: Conversation, task: GeneralTask) -> Path:
        directory = self.store.task_directory(conversation.id, task.id, task.output_root)
        if str(directory) != task.result_directory:
            raise ValueError("Task result directory does not match its persisted identity")
        return directory

    def _reconcile(self, task: GeneralTask, directory: Path) -> None:
        for run in task.analysis_runs:
            if load_run(directory, run.id, run.spec) != run:
                raise ValueError("Recorded analysis receipt is missing or inconsistent")
        for node in task.nodes:
            for call in (item for item in task.calls if item.node_id == node.id):
                if call.status not in ("running", "interrupted"):
                    continue
                spec = self._execution_spec(task, node)
                if (
                    call.tool != spec.capability or call.tool_version != spec.capability_version
                    or call.baseline_version != task.baseline.version or call.inputs != spec.inputs
                ):
                    raise ValueError("Pending call does not match its confirmed scope")
                saved = load_run(directory, call.id, spec)
                if saved is None:
                    raise ValueError(
                        "Pending capability execution state is unknown; do not redispatch"
                    )
                self._accept(task, node, call, saved, directory)
            if node.status == "completed":
                completed_run = next((item for item in task.analysis_runs if item.id in {
                    call.id for call in task.calls
                    if call.node_id == node.id and call.status == "completed"
                }), None)
                if (
                    completed_run is None or task.outputs.get(node.id) != completed_run.output
                    or completed_run.spec != self._execution_spec(task, node)
                ):
                    raise ValueError("Completed evidence does not match its confirmed baseline")

    def _execution_spec(self, task: GeneralTask, node: ExecutionNode) -> AnalysisSpec:
        spec = task.baseline.specs[node.id]
        if not node.depends_on:
            return spec
        return spec.model_copy(update={"conditions": {
            "dependency_output_sha256": {
                name: fingerprint(task.outputs[name]) for name in node.depends_on
            }
        }})

    def _accept(
        self, task: GeneralTask, node: ExecutionNode, call: ToolCall,
        run: AnalysisRun, directory: Path,
    ) -> None:
        if not any(item.id == run.id for item in task.analysis_runs):
            task.analysis_runs.append(run)
        task.outputs[node.id] = run.output
        call.output = run.output
        call.status = "completed"
        call.error = None
        call.finished_at = run.finished_at
        node.status = "completed"
        node.error = None
        artifact = describe_artifact(directory, receipt_name(run.id))
        if artifact not in task.artifacts:
            task.artifacts.append(artifact)

    def run(self, conversation: Conversation, task: GeneralTask) -> Conversation:
        scope = self._scope(task)
        if not scope.confirmed:
            raise ValueError("Execution requires a confirmed baseline")
        conversation.turn_origin = None
        conversation.status = "running"
        conversation.error = None
        conversation.pending_decision = None
        task.started_at = task.started_at or now()
        self.store.save(conversation)
        active_node: ExecutionNode | None = None
        active_call: ToolCall | None = None
        try:
            directory = self._directory(conversation, task)
            self._reconcile(task, directory)
            self._validate_pending(task)
            for artifact in task.artifacts:
                verify_artifact(directory, artifact)

            def decide() -> LoopDecision:
                if all(node.status == "completed" for node in task.nodes):
                    return LoopDecision(action="stop", reason="All confirmed outputs are recorded")
                elapsed = (
                    datetime.now(UTC) - datetime.fromisoformat(task.started_at or "")
                ).total_seconds()
                if elapsed >= task.baseline.budget.max_seconds:
                    raise ValueError("The confirmed elapsed-time budget is exhausted")
                if len(task.calls) >= task.baseline.budget.max_calls:
                    raise ValueError("The confirmed call budget is exhausted")
                completed = {node.id for node in task.nodes if node.status == "completed"}
                ready = next((
                    node for node in task.nodes
                    if node.status == "pending" and set(node.depends_on) <= completed
                ), None)
                if ready is None:
                    raise ValueError(
                        "No safe pending node is ready; failed or unknown calls need review"
                    )
                return LoopDecision(
                    action="continue", effect=self.tools.definition(ready.tool).effect,
                    reason=f"Execute confirmed node {ready.id}",
                )

            def execute() -> None:
                nonlocal active_node, active_call
                completed = {node.id for node in task.nodes if node.status == "completed"}
                active_node = next(
                    node for node in task.nodes
                    if node.status == "pending" and set(node.depends_on) <= completed
                )
                base_spec = task.baseline.specs[active_node.id]
                if self._spec(task.proposal, next(
                    node for node in task.proposal.nodes if node.id == active_node.id
                )) != base_spec:
                    raise ValueError("Pending capability version or inputs changed before dispatch")
                spec = self._execution_spec(task, active_node)
                active_call = ToolCall(
                    id=uuid4().hex, node_id=active_node.id, tool=spec.capability,
                    tool_version=spec.capability_version, baseline_version=task.baseline.version,
                    started_at=now(), inputs=spec.inputs,
                )
                task.calls.append(active_call)
                active_node.status = "running"
                task.stage = active_node.id
                self.store.save(conversation)
                output = self.tools.invoke(
                    spec.capability, spec.inputs,
                    GeneralContext(
                        directory=directory, scope=scope,
                        outputs=deepcopy({
                            name: dict(task.outputs[name]) for name in active_node.depends_on
                        }),
                    ),
                    scope,
                )
                run = AnalysisRun(
                    id=active_call.id, spec=spec, output=output,
                    output_sha256=fingerprint(output),
                    started_at=active_call.started_at, finished_at=now(),
                )
                persist_run(directory, run)
                self._accept(task, active_node, active_call, run, directory)
                active_node = None
                active_call = None

            run_loop(
                decide=decide, execute=execute, persist=lambda: self.store.save(conversation),
                scope=scope, max_steps=task.baseline.budget.max_calls,
            )
            scope.authorize("write_delivery")
            task.stage = "completed"
            _write_json(directory / "execution_record.json", task.model_dump(mode="json"))
            task.artifacts = [
                item for item in task.artifacts if item.path != "execution_record.json"
            ] + [describe_artifact(directory, "execution_record.json")]
            conversation.status = "completed"
            self._message(conversation, "已完成已确认范围内的能力调用；输出与版本回执已记录。")
        except (OSError, ValueError, RuntimeError) as exc:
            logger.exception("General execution failed for %s", task.id)
            if active_node is not None:
                active_node.status = "failed"
                active_node.error = str(exc)[:4000]
            if active_call is not None:
                active_call.status = "failed"
                active_call.error = str(exc)[:4000]
                active_call.finished_at = now()
            conversation.status = "failed"
            conversation.error = str(exc)[:4000]
            conversation.pending_decision = PendingDecision(
                kind="resume",
                message="能力执行或交付已停止，请核对已保存的证据：" + str(exc)[:2000],
            )
            self._message(conversation, conversation.pending_decision.message)
        self.store.save(conversation)
        return conversation

    def _message(self, conversation: Conversation, content: str) -> None:
        conversation.messages.append(
            ConversationMessage(role="assistant", content=content, created_at=now())
        )
