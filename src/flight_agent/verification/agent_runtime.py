import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from ..config import SchedulerSettings
from ..runtime.agent_contracts import (
    AgentBaseline,
    AgentNode,
    AgentPlan,
    AgentScope,
    BudgetState,
    InvocationRun,
    InvocationSpec,
    resolve_inputs,
    validate_bindings,
)
from ..runtime.budget import BudgetAccount, BudgetExhausted
from ..runtime.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    validate_declared_outcome,
)
from ..runtime.contracts import fingerprint
from ..runtime.conversation_locks import ConversationLockRegistry
from ..runtime.evidence import (
    MAX_EVIDENCE_BYTES,
    agent_receipt_name,
    describe_evidence,
    load_agent_run,
    persist_agent_run,
    publish_evidence,
    verify_evidence,
)
from ..runtime.loop import LoopDecision, run_loop
from ..runtime.observations import (
    CapabilityOutcome,
    OutputArtifact,
    TaskResult,
    combine_outcomes,
)
from ..runtime.project_snapshots import ProjectSnapshot
from ..runtime.requirements import CapabilityGap
from ..runtime.resources import ResourceReference, bounded_json
from ..runtime.scheduling import ExecutionScheduler
from ..runtime.scheduling_contracts import SchedulingMetadata
from ..runtime.tasks import plan_ancestors
from .contracts import (
    AgentExecutionNode,
    AgentTask,
    Conversation,
    ConversationMessage,
    ConversationState,
    Decision,
    PendingDecision,
    ToolCall,
)
from .conversation_repository import ConversationRepository
from .intake import safe_directory, validate_output_root
from .project_snapshots import ProjectSnapshotStore
from .resources import ResourceRegistry
from .snapshot_binding import (
    local_projects,
    prepare_project_snapshots,
    snapshot_lineage,
    validate_managed_boundaries,
    validate_snapshot_bindings,
    verify_execution_resources,
    verify_snapshot_lineage,
)
from .store import now

logger = logging.getLogger(__name__)
MAX_CHECKPOINT_BYTES = 4_000_000


def has_unresolved_call(task: AgentTask) -> bool:
    return any(call.status in ("running", "interrupted") for call in task.calls)


class ExecutionUncertain(RuntimeError):
    """A dispatched operation has no proven terminal state."""


class ReconciledFailure(RuntimeError):
    """A reconciliation handler has verified the original operation failed."""


class PreDispatchBudgetExhausted(BudgetExhausted):
    """The capability has verified that no external operation was submitted."""


@dataclass
class OperationReservation:
    action: str
    task_id: str
    queue_origin: ConversationState
    stamp: str
    node_id: str
    node_tool: str
    call_id: str
    spec: InvocationSpec
    definition: CapabilityDefinition
    scheduling: SchedulingMetadata
    failure_status: str | None = None
    failure_error: str | None = None


@dataclass
class RunState:
    operation: OperationReservation | None = None


@dataclass(frozen=True)
class _PreparedDecision:
    task_id: str | None
    save: bool


@dataclass(frozen=True)
class AgentContext:
    directory: Path
    scope: AgentScope
    resources: tuple[ResourceReference, ...]
    node_id: str
    call_id: str
    outputs: dict[str, CapabilityOutcome]
    budget: BudgetAccount
    load_checkpoint: Callable[[], dict[str, JsonValue] | None]
    save_checkpoint: Callable[[dict[str, JsonValue]], None]
    _resource_roles: dict[str, ResourceReference]
    work_directory: Path | None = None
    _project_snapshots: dict[str, ProjectSnapshot] = dataclass_field(
        default_factory=dict
    )

    def resource(self, role: str) -> ResourceReference:
        if role not in self._resource_roles:
            raise ValueError(f"Resource role is outside this node's bindings: {role}")
        return ResourceReference.model_validate(self._resource_roles[role])

    def project_snapshot(self, role: str) -> ProjectSnapshot:
        resource = self.resource(role)
        if resource.id not in self._project_snapshots:
            raise ValueError("Local project requires its confirmed managed snapshot")
        binding = self._project_snapshots[resource.id]
        if binding.source_fingerprint != resource.fingerprint:
            raise ValueError("Snapshot differs from the bound resource fingerprint")
        return ProjectSnapshot.model_validate(binding.model_dump(mode="json"))


def _node(node: AgentNode) -> AgentNode:
    return AgentNode.model_validate(node.model_dump(include=set(AgentNode.model_fields)))


def _scope(baseline: AgentBaseline, node: AgentNode | None = None) -> AgentScope:
    identifiers = set(node.resource_bindings.values()) if node is not None else None
    return AgentScope(
        baseline_version=baseline.version,
        confirmed=baseline.confirmed,
        resources=[
            resource
            for resource in baseline.resources
            if identifiers is None or resource.id in identifiers
        ],
        allowed_effects=baseline.allowed_effects,
    )


def build_invocation_spec(
    baseline: AgentBaseline,
    node: AgentNode,
    inputs: dict[str, JsonValue],
    outputs: dict[str, CapabilityOutcome],
) -> InvocationSpec:
    snapshot = baseline.capability_snapshots[node.tool]
    by_id = {resource.id: resource for resource in baseline.resources}
    resources = {role: by_id[identifier] for role, identifier in node.resource_bindings.items()}
    lineage = snapshot_lineage(baseline, node)
    snapshot_numerics: dict[str, JsonValue] = {}
    if lineage:
        snapshot_numerics["project_snapshots"] = {
            identifier: binding.model_dump(mode="json")
            for identifier, binding in lineage.items()
        }
    conditions = inputs.get("conditions", {})
    return InvocationSpec(
        capability=node.tool,
        capability_version=node.tool_version,
        inputs=inputs,
        resource_fingerprints={
            resource.id: resource.fingerprint for resource in resources.values()
        },
        conditions=conditions if isinstance(conditions, dict) else {},
        numerics={
            "capability_declaration_sha256": fingerprint(snapshot.model_dump(mode="json")),
            "confirmed_inputs_sha256": fingerprint(resolve_inputs(node, outputs)),
            "validity_policy": snapshot.metadata.validity_policy,
            "resources": {
                role: resource.model_dump(mode="json") for role, resource in resources.items()
            },
            **snapshot_numerics,
        },
        dependency_output_sha256={
            name: fingerprint(outputs[name].model_dump(mode="json")) for name in node.depends_on
        },
    )


def build_invocation_run(
    call: ToolCall, spec: InvocationSpec, output: dict[str, JsonValue]
) -> InvocationRun:
    outcome = CapabilityOutcome.model_validate(output)
    return InvocationRun(
        id=call.id,
        spec=spec,
        output=outcome,
        output_sha256=fingerprint(outcome.model_dump(mode="json")),
        started_at=call.started_at,
        finished_at=now(),
        artifacts=outcome.artifacts,
    )


def persist_running_call(
    conversation: Conversation,
    task: AgentTask,
    node: AgentExecutionNode,
    spec: InvocationSpec,
    *,
    store: ConversationRepository,
) -> ToolCall:
    call = ToolCall(
        id=uuid4().hex,
        node_id=node.id,
        tool=spec.capability,
        tool_version=spec.capability_version,
        baseline_version=task.baseline.version,
        started_at=now(),
        inputs=spec.inputs,
    )
    task.calls.append(call)
    task.pending_specs[call.id] = spec
    node.status = "running"
    node.error = None
    task.stage = node.id
    store.save(conversation)
    return call


def accept_verified_run(
    task: AgentTask, node: AgentExecutionNode, call: ToolCall, run: InvocationRun
) -> None:
    spec = task.pending_specs.get(call.id)
    if (
        run.id != call.id
        or spec is None
        or run.spec != spec
        or call.inputs != spec.inputs
        or call.node_id != node.id
        or call.tool != spec.capability
        or call.tool_version != spec.capability_version
        or call.baseline_version != task.baseline.version
    ):
        raise ValueError("Invocation receipt does not match its recorded confirmed call")
    outcome = _validate_outcome(task, node, run.output)
    if any(existing.id == run.id and existing != run for existing in task.analysis_runs):
        raise ValueError("Invocation receipt is immutable")
    if not any(existing.id == run.id for existing in task.analysis_runs):
        task.analysis_runs.append(run)
    task.outputs[node.id] = outcome
    call.output = outcome.model_dump(mode="json")
    call.status = "completed"
    call.error = None
    call.finished_at = run.finished_at
    node.status = "completed"
    node.error = None
    task.pending_specs.pop(call.id)


def _validate_outcome(
    task: AgentTask, node: AgentExecutionNode, output: object
) -> CapabilityOutcome:
    outcome = validate_declared_outcome(
        task.baseline.capability_snapshots[node.tool].metadata, output, node_id=node.id
    )
    if outcome.evaluation is not None:
        if task.baseline.evaluation_node != node.id:
            raise ValueError("Evaluation output is outside the confirmed evaluation node")
        outcome.evaluation.validate_requirements(task.baseline.requirements, complete=True)
    combine_outcomes({**task.outputs, node.id: outcome})
    return outcome


def build_context(
    conversation: Conversation,
    task: AgentTask,
    node: AgentExecutionNode,
    call: ToolCall,
    budget: BudgetAccount,
    *,
    store: ConversationRepository,
    locks: ConversationLockRegistry,
    action: str,
    queue_origin: ConversationState,
    operation: OperationReservation,
    snapshots: ProjectSnapshotStore | None = None,
) -> AgentContext:
    conversation_id = conversation.id
    task_id = task.id
    node_id = node.id
    call_id = call.id
    directory = store.task_directory(conversation.id, task.id, task.output_root)
    if str(directory) != task.result_directory:
        raise ValueError("Task result directory does not match its persisted identity")
    work_directory = (
        store.root
        / "work"
        / conversation.id
        / task.id
        / "calls"
        / call.id
    )
    safe_directory(work_directory)
    spec = task.pending_specs[call.id]
    checkpoint_root = f"agent/checkpoints/{call.id}"

    def active() -> tuple[Conversation, AgentTask, ToolCall]:
        current = store.get(conversation_id)
        current_task = current.task
        if (
            current_task is None
            or current_task.id != task_id
            or current.status != "running"
            or current.queued_action != action
            or current.queue_origin != queue_origin
            or current.updated_at != operation.stamp
            or current_task.stage != node_id
        ):
            raise ValueError("Checkpoint access is outside the admitted action")
        current_call = next(
            (item for item in current_task.calls if item.id == call_id), None
        )
        current_node = next(
            (item for item in current_task.nodes if item.id == node_id), None
        )
        if (
            current_call is None
            or current_node is None
            or current_call.node_id != node_id
            or current_call.status not in ("running", "interrupted")
            or current_task.pending_specs.get(call_id) != spec
        ):
            raise ValueError("Checkpoint access is outside the original unresolved call")
        current_name = current_task.checkpoints.get(call_id)
        if (
            current_name is not None
            and Path(current_name).parent.as_posix() != checkpoint_root
        ):
            raise ValueError("Checkpoint reference is outside this call's directory")
        return current, current_task, current_call

    def read_checkpoint(
        current_task: AgentTask,
    ) -> dict[str, JsonValue] | None:
        if call_id not in current_task.checkpoints:
            return None
        name = current_task.checkpoints[call_id]
        descriptor = describe_evidence(directory, name)
        if descriptor.sha256 != current_task.checkpoint_sha256.get(call_id):
            raise ValueError("Checkpoint digest does not match the recorded state")
        if descriptor.size > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds its size limit")
        with (directory / name).open("rb") as stream:
            content = stream.read(MAX_CHECKPOINT_BYTES + 1)
        if len(content) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds its size limit")
        if hashlib.sha256(content).hexdigest() != descriptor.sha256:
            raise ValueError("Checkpoint digest changed during reading")
        payload = json.loads(content)
        if (
            not isinstance(payload, dict)
            or payload.get("call_id") != call_id
            or payload.get("spec_fingerprint") != spec.fingerprint
            or not isinstance(payload.get("data"), dict)
        ):
            raise ValueError("Checkpoint identity does not match the original call")
        bounded_json(payload)
        fingerprint(payload)
        return payload["data"]

    def load_checkpoint() -> dict[str, JsonValue] | None:
        with locks.hold(conversation_id):
            _, current_task, _ = active()
            return read_checkpoint(current_task)

    def save_checkpoint(data: dict[str, JsonValue]) -> None:
        if not isinstance(data, dict):
            raise ValueError("Checkpoint data must be a JSON object")
        payload = {
            "call_id": call_id,
            "spec_fingerprint": spec.fingerprint,
            "data": data,
        }
        bounded_json(payload)
        fingerprint(payload)
        content = (
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(content) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds its serialized size limit")
        with locks.hold(conversation_id):
            current, current_task, _ = active()
            if call_id in current_task.checkpoints:
                read_checkpoint(current_task)
            digest = hashlib.sha256(content).hexdigest()
            name = f"{checkpoint_root}/{digest}.json"
            publish_evidence(directory, name, content)
            descriptor = describe_evidence(directory, name)
            current_task.checkpoints[call_id] = name
            current_task.checkpoint_sha256[call_id] = descriptor.sha256
            store.save(current)
            operation.stamp = current.updated_at

    resources = {resource.id: resource for resource in task.baseline.resources}
    roles = {
        role: ResourceReference.model_validate(resources[identifier])
        for role, identifier in node.resource_bindings.items()
    }
    new_style = "project_snapshots" in spec.numerics
    lineage = (
        snapshot_lineage(task.baseline, _node(node))
        if new_style
        else {}
    )
    if snapshots is not None and new_style:
        verify_snapshot_lineage(task.baseline, _node(node), snapshots)
    return AgentContext(
        directory=directory,
        scope=_scope(task.baseline, node),
        resources=tuple(roles.values()),
        node_id=node.id,
        call_id=call.id,
        outputs={name: task.outputs[name].model_copy(deep=True) for name in node.depends_on},
        budget=budget,
        load_checkpoint=load_checkpoint,
        save_checkpoint=save_checkpoint,
        _resource_roles=roles,
        work_directory=work_directory,
        _project_snapshots=lineage,
    )


class AgentExecutor:
    def __init__(
        self,
        store: ConversationRepository,
        tools: CapabilityRegistry[AgentContext],
        resources: ResourceRegistry,
        *,
        locks: ConversationLockRegistry | None = None,
        scheduler: ExecutionScheduler | None = None,
        snapshots: ProjectSnapshotStore | None = None,
    ) -> None:
        self.store = store
        self.tools = tools
        self.resources = resources
        self.locks = locks if locks is not None else ConversationLockRegistry()
        self.scheduler = (
            scheduler
            if scheduler is not None
            else ExecutionScheduler(SchedulerSettings.from_environment())
        )
        self.snapshots = (
            snapshots
            if snapshots is not None
            else ProjectSnapshotStore(store.root / "snapshots")
        )
        self.work_root = store.root / "work"

    def _directory(
        self, conversation: Conversation, task: AgentTask, *, create: bool = False
    ) -> Path:
        if task.output_root is not None and "write_delivery" not in task.baseline.allowed_effects:
            raise ValueError("External output requires the confirmed write_delivery permission")
        expected = (
            Path(task.output_root) / f"flight-agent-{task.id}"
            if task.output_root
            else self.store.default_task_root(conversation.id) / task.id
        )
        if str(expected) != task.result_directory:
            raise ValueError("Task result directory does not match its persisted identity")
        return self.store.task_directory(
            conversation.id, task.id, task.output_root, create=create and not expected.exists()
        )

    @staticmethod
    def _task(conversation: Conversation) -> AgentTask:
        if not isinstance(conversation.task, AgentTask):
            raise ValueError("No generic v2 task is available for this operation")
        return conversation.task

    @staticmethod
    def _task_by_id(
        conversation: Conversation, task_id: str
    ) -> AgentTask | None:
        return next(
            (
                task
                for task in [
                    *conversation.archived_tasks,
                    *([conversation.task] if conversation.task else []),
                ]
                if task.id == task_id
            ),
            None,
        )

    @staticmethod
    def _validate_structure(task: AgentTask) -> None:
        baseline = AgentBaseline.model_validate(task.baseline)
        if [_node(node) for node in task.nodes] != baseline.nodes:
            raise ValueError("Execution nodes differ from the confirmed baseline")
        if set(baseline.capability_snapshots) != {node.tool for node in baseline.nodes}:
            raise ValueError("Baseline snapshots must cover exactly the confirmed capabilities")
        validate_snapshot_bindings(baseline, require=False)
        if baseline.confirmed and (
            task.baseline_sha256 != fingerprint(baseline.model_dump(mode="json"))
        ):
            raise ValueError("Baseline differs from its recorded confirmation fingerprint")

    def _validate_resource_boundary(self, baseline: AgentPlan, output: str | None) -> None:
        validate_managed_boundaries(
            baseline,
            self.resources,
            self.snapshots,
            self.work_root,
            output,
        )

    def propose(self, conversation: Conversation, plan: AgentPlan) -> Conversation:
        with self.locks.hold(conversation.id):
            conversation = self.store.get(conversation.id)
            if conversation.status in ("queued", "running"):
                raise ValueError("This conversation already has an admitted turn")
            tasks = [
                *conversation.archived_tasks,
                *([conversation.task] if conversation.task else []),
            ]
            if any(has_unresolved_call(task) for task in tasks):
                raise ValueError(
                    "Reconcile unresolved or unknown calls before proposing a new plan"
                )
            plan = AgentPlan.model_validate(plan)
            plan.check_dispatch_budget()
            validate_bindings(plan, self.tools)
            self.resources.verify(plan.resources)
            output_root = validate_output_root(conversation.output_root)
            if output_root is not None and "write_delivery" not in plan.allowed_effects:
                raise ValueError("External output requires the write_delivery permission")
            task_id = uuid4().hex
            effective = (
                str(Path(output_root) / f"flight-agent-{task_id}")
                if output_root
                else str(self.store.default_task_root(conversation.id) / task_id)
            )
            self._validate_resource_boundary(plan, output_root or effective)
            baseline = AgentBaseline(
                **plan.model_dump(),
                version=max((task.baseline.version for task in tasks), default=0) + 1,
                capability_snapshots={
                    node.tool: self.tools.snapshot(node.tool) for node in plan.nodes
                },
            )
            task = AgentTask(
                id=task_id,
                baseline=baseline,
                nodes=[AgentExecutionNode(**node.model_dump()) for node in plan.nodes],
                result_directory=effective,
                output_root=output_root,
            )
            self._validate_structure(task)
            if conversation.task is not None:
                conversation.archived_tasks.append(conversation.task)
            conversation.task = task
            conversation.output_root = output_root
            conversation.turn_origin = None
            conversation.queued_action = None
            conversation.queued_action_committed = False
            conversation.queue_origin = None
            conversation.status = "waiting_for_user"
            conversation.error = None
            conversation.pending_decision = PendingDecision(
                kind="confirm_baseline",
                message=(
                    f"请核对执行方案 v{baseline.version} 的需求、来源、工具、"
                    "资源、权限与预算后确认。"
                ),
            )
            conversation.messages.append(
                ConversationMessage(
                    role="assistant",
                    content=conversation.pending_decision.message,
                    created_at=now(),
                )
            )
            self.store.save(conversation)
            return conversation

    def confirm(
        self,
        conversation_id: str,
        version: int,
        *,
        trusted: bool,
        _execute: bool = True,
    ) -> Conversation:
        with self.locks.hold(conversation_id):
            conversation = self.store.get(conversation_id)
            conversation, prepared = self._confirm_and_save(
                conversation,
                version,
                trusted=trusted,
            )
        if not _execute or prepared.task_id is None:
            return conversation
        return self._run(conversation_id, "confirm", prepared.task_id)

    def _confirm_and_save(
        self,
        conversation: Conversation,
        version: int,
        *,
        trusted: bool,
        revalidate: Callable[[], None] | None = None,
    ) -> tuple[Conversation, _PreparedDecision]:
        candidate = conversation.model_copy(deep=True)
        prepared = self._prepare_confirm(candidate, version, trusted=trusted)
        if not prepared.save:
            return conversation, prepared
        task = self._task(candidate)
        guard = self.snapshots.guard() if local_projects(task.baseline) else nullcontext()
        with guard:
            bindings = prepare_project_snapshots(task.baseline, self.snapshots)
            self.resources.verify(task.baseline.resources)
            task.baseline = AgentBaseline.model_validate(
                {
                    **task.baseline.model_dump(mode="json"),
                    "project_snapshots": {
                        identifier: binding.model_dump(mode="json")
                        for identifier, binding in bindings.items()
                    },
                    "confirmed": True,
                    "confirmed_at": now(),
                }
            )
            validate_snapshot_bindings(task.baseline, require=True)
            task.baseline_sha256 = fingerprint(task.baseline.model_dump(mode="json"))
            task.decisions.append(
                Decision(
                    action="confirm_baseline",
                    baseline_version=version,
                    trusted=True,
                    created_at=now(),
                )
            )
            candidate.queue_origin = ConversationState(
                status=candidate.status,
                pending_decision=candidate.pending_decision,
                error=candidate.error,
            )
            candidate.status = "queued"
            candidate.queued_action = "confirm"
            candidate.queued_action_committed = True
            candidate.pending_decision = None
            candidate.error = None
            if revalidate is not None:
                revalidate()
            self.store.save(candidate)
        return candidate, prepared

    def _prepare_confirm(
        self,
        conversation: Conversation,
        version: int,
        *,
        trusted: bool,
    ) -> _PreparedDecision:
        task = self._task(conversation)
        if trusted is not True or type(version) is not int or version != task.baseline.version:
            raise ValueError(
                "Explicit consent for the exact current baseline version is required"
            )
        if conversation.status in ("queued", "planning", "running"):
            if conversation.queued_action == "confirm":
                return _PreparedDecision(task_id=None, save=False)
            raise ValueError("This conversation already has an admitted turn")
        self._validate_structure(task)
        if task.baseline.confirmed:
            if conversation.status == "completed":
                return _PreparedDecision(task_id=None, save=False)
            raise ValueError("The baseline is already confirmed; resume the original task")
        task.baseline.check_dispatch_budget()
        validate_bindings(task.baseline, self.tools)
        self.resources.verify(task.baseline.resources)
        self._validate_resource_boundary(
            task.baseline,
            task.output_root or task.result_directory,
        )
        self._directory(conversation, task, create=True)
        return _PreparedDecision(task_id=task.id, save=True)

    def resume(
        self, conversation_id: str, *, _execute: bool = True
    ) -> Conversation:
        with self.locks.hold(conversation_id):
            conversation = self.store.get(conversation_id)
            prepared = self._prepare_resume(conversation)
            if prepared.save:
                self.store.save(conversation)
        if not _execute or prepared.task_id is None:
            return conversation
        return self._run(conversation_id, "resume", prepared.task_id)

    def _prepare_resume(self, conversation: Conversation) -> _PreparedDecision:
        task = self._task(conversation)
        if conversation.status in ("queued", "planning", "running"):
            if conversation.queued_action == "resume":
                return _PreparedDecision(task_id=None, save=False)
            raise ValueError("This conversation already has an admitted turn")
        if not task.baseline.confirmed:
            raise ValueError("Execution requires a confirmed baseline")
        self._validate_structure(task)
        if (
            task.result is not None
            and task.result.execution_status in ("completed", "partial")
            and all(node.status == "completed" for node in task.nodes)
        ):
            try:
                self._verify_saved(conversation, task)
                conversation.status = "completed"
                conversation.error = None
                conversation.pending_decision = None
            except (OSError, ValueError, RuntimeError) as exc:
                conversation.status = "failed"
                conversation.error = str(exc)[:4000]
                conversation.pending_decision = PendingDecision(
                    kind="retry",
                    message=conversation.error,
                )
            return _PreparedDecision(task_id=None, save=True)
        task.decisions.append(
            Decision(action="resume", baseline_version=task.baseline.version, created_at=now())
        )
        conversation.queue_origin = ConversationState(
            status=conversation.status,
            pending_decision=conversation.pending_decision,
            error=conversation.error,
        )
        conversation.status = "queued"
        conversation.queued_action = "resume"
        conversation.queued_action_committed = True
        conversation.pending_decision = None
        conversation.error = None
        return _PreparedDecision(task_id=task.id, save=True)

    def retry(
        self,
        conversation_id: str,
        version: int,
        *,
        trusted: bool,
        _execute: bool = True,
    ) -> Conversation:
        with self.locks.hold(conversation_id):
            conversation = self.store.get(conversation_id)
            prepared = self._prepare_retry(conversation, version, trusted=trusted)
            if prepared.save:
                self.store.save(conversation)
        if not _execute or prepared.task_id is None:
            return conversation
        return self._run(conversation_id, "retry", prepared.task_id)

    def _prepare_retry(
        self,
        conversation: Conversation,
        version: int,
        *,
        trusted: bool,
    ) -> _PreparedDecision:
        task = self._task(conversation)
        if trusted is not True or type(version) is not int or version != task.baseline.version:
            raise ValueError("Retry requires explicit consent for the current baseline version")
        if conversation.status in ("queued", "planning", "running"):
            if conversation.queued_action == "retry":
                return _PreparedDecision(task_id=None, save=False)
            raise ValueError("This conversation already has an admitted turn")
        if has_unresolved_call(task):
            raise ValueError("Reconcile unknown or unresolved calls before retrying")
        self._validate_structure(task)
        if not task.baseline.confirmed or not any(
            node.status == "failed" for node in task.nodes
        ):
            raise ValueError("No known failed call is eligible for explicit retry")
        for node in task.nodes:
            if node.status == "failed":
                node.status = "pending"
                node.error = None
        task.decisions.append(
            Decision(action="retry", baseline_version=version, trusted=True, created_at=now())
        )
        conversation.queue_origin = ConversationState(
            status=conversation.status,
            pending_decision=conversation.pending_decision,
            error=conversation.error,
        )
        conversation.status = "queued"
        conversation.queued_action = "retry"
        conversation.queued_action_committed = True
        conversation.pending_decision = None
        conversation.error = None
        return _PreparedDecision(task_id=task.id, save=True)

    def _prepare_decision(
        self,
        conversation: Conversation,
        action: str,
        *,
        version: int | None,
        trusted: bool,
    ) -> _PreparedDecision:
        if action == "confirm":
            return self._prepare_confirm(
                conversation,
                version if version is not None else 0,
                trusted=trusted,
            )
        if action == "resume":
            return self._prepare_resume(conversation)
        if action == "retry":
            return self._prepare_retry(
                conversation,
                version if version is not None else 0,
                trusted=trusted,
            )
        raise ValueError("Submit new requirements for a new confirmed plan")

    def _live_node(self, task: AgentTask, node: AgentExecutionNode) -> None:
        self._validate_structure(task)
        current = self.tools.snapshot(node.tool)
        expected = task.baseline.capability_snapshots[node.tool]
        if current != expected:
            raise ValueError("Capability declaration differs from its confirmed snapshot")
        _scope(task.baseline, node).authorize(current.effect)
        self.tools.validate_plan(task.baseline, _node(node))

    def _spec(self, task: AgentTask, node: AgentExecutionNode, call: ToolCall) -> InvocationSpec:
        return build_invocation_spec(task.baseline, _node(node), call.inputs, task.outputs)

    def _spec_matches(
        self,
        task: AgentTask,
        node: AgentExecutionNode,
        call: ToolCall,
        spec: InvocationSpec | None,
    ) -> bool:
        if spec is None:
            return False
        expected = self._spec(task, node, call)
        if spec == expected:
            return True
        if "project_snapshots" in spec.numerics:
            return False
        numerics = dict(expected.numerics)
        numerics.pop("project_snapshots", None)
        legacy = InvocationSpec.model_validate(
            {
                **expected.model_dump(mode="json"),
                "numerics": numerics,
            }
        )
        return spec == legacy

    def _verified_run(
        self,
        conversation: Conversation,
        task: AgentTask,
        node: AgentExecutionNode,
        call: ToolCall,
        spec: InvocationSpec,
    ) -> InvocationRun:
        run = load_agent_run(self._directory(conversation, task), call.id, spec)
        if run is None:
            raise ValueError("Recorded invocation receipt is missing")
        _validate_outcome(task, node, run.output)
        return run

    def _verify_saved(self, conversation: Conversation, task: AgentTask) -> None:
        completed = {node.id for node in task.nodes if node.status == "completed"}
        if set(task.outputs) != completed:
            raise ValueError("Stored evidence does not match the completed nodes")
        completed_calls = {call.id for call in task.calls if call.status == "completed"}
        if {run.id for run in task.analysis_runs} != completed_calls:
            raise ValueError("Completed calls lack their recorded invocation receipts")
        for node in task.nodes:
            if node.status != "completed":
                continue
            call = next(
                (
                    call
                    for call in reversed(task.calls)
                    if call.node_id == node.id and call.status == "completed"
                ),
                None,
            )
            if call is None:
                raise ValueError("Completed node has no matching recorded call")
            recorded = next(
                (run for run in task.analysis_runs if run.id == call.id),
                None,
            )
            if recorded is None:
                raise ValueError("Completed call lacks its recorded invocation receipt")
            spec = recorded.spec
            if (
                not self._spec_matches(task, node, call, spec)
                or call.inputs != spec.inputs
                or call.tool != node.tool
                or call.tool_version != node.tool_version
                or call.baseline_version != task.baseline.version
            ):
                raise ValueError("Completed call differs from its confirmed baseline")
            run = self._verified_run(conversation, task, node, call, spec)
            if run not in task.analysis_runs or run.output != task.outputs[node.id]:
                raise ValueError("Stored evidence differs from its verified receipt")
            if run.reused_from is not None:
                source = self._source(conversation, spec, run.reused_from)
                if source is None or task.reuse_sources.get(run.id) != run.reused_from:
                    raise ValueError("Derived receipt has no verified original source")
                original_task, original, original_node = source
                if task.reuse_mappings.get(run.id) != {original_node.id: node.id}:
                    raise ValueError("Derived receipt lacks its producer mapping")
                if self._remap(original.output, original_node.id, node.id) != run.output:
                    raise ValueError("Derived output differs from the original evidence")
        for artifact in task.artifacts:
            verify_evidence(self._directory(conversation, task), artifact)

    def _source(
        self, conversation: Conversation, spec: InvocationSpec, run_id: str | None = None
    ) -> tuple[AgentTask, InvocationRun, AgentExecutionNode] | None:
        for task in reversed(conversation.archived_tasks):
            if not isinstance(task, AgentTask):
                continue
            self._validate_structure(task)
            for original in reversed(task.analysis_runs):
                if original.reused_from is not None:
                    continue
                if original.spec.fingerprint != spec.fingerprint or (
                    run_id is not None and original.id != run_id
                ):
                    continue
                call = next((call for call in task.calls if call.id == original.id), None)
                node = next(
                    (node for node in task.nodes if call is not None and node.id == call.node_id),
                    None,
                )
                if call is None or call.status != "completed" or node is None:
                    raise ValueError("Cached receipt has no completed original call")
                if not self._spec_matches(task, node, call, spec):
                    raise ValueError("Cached receipt differs from its original confirmed scope")
                if "project_snapshots" in spec.numerics:
                    verify_snapshot_lineage(
                        task.baseline,
                        _node(node),
                        self.snapshots,
                    )
                verified = self._verified_run(conversation, task, node, call, spec)
                if verified != original or task.outputs.get(node.id) != verified.output:
                    raise ValueError("Cached evidence differs from its original receipt")
                return task, verified, node
        return None

    @staticmethod
    def _remap(output: CapabilityOutcome, old: str, new: str) -> CapabilityOutcome:
        data = output.model_dump(mode="json")
        for field in ("observations", "findings", "tables", "artifacts"):
            for item in data[field]:
                if item.get("node_id") == old:
                    item["node_id"] = new
        return CapabilityOutcome.model_validate(data)

    def _reuse(
        self,
        conversation: Conversation,
        task: AgentTask,
        node: AgentExecutionNode,
        call: ToolCall,
        spec: InvocationSpec,
        source: tuple[AgentTask, InvocationRun, AgentExecutionNode],
    ) -> InvocationRun:
        original_task, original, original_node = source
        task.reuse_sources[call.id] = original.id
        task.reuse_mappings[call.id] = {original_node.id: node.id}
        self.store.save(conversation)
        source_root = self._directory(conversation, original_task)
        target_root = self._directory(conversation, task)
        for artifact in [*original.artifacts, *original.output.artifacts]:
            path = verify_evidence(source_root, artifact)
            with path.open("rb") as stream:
                content = stream.read(MAX_EVIDENCE_BYTES + 1)
            publish_evidence(target_root, artifact.path, content)
            verify_evidence(source_root, artifact)
            verify_evidence(target_root, artifact)
        outcome = self._remap(original.output, original_node.id, node.id)
        run = build_invocation_run(call, spec, outcome.model_dump(mode="json"))
        run = InvocationRun.model_validate(
            {
                **run.model_dump(),
                "reused_from": original.id,
            }
        )
        persist_agent_run(target_root, run)
        return run

    def _accept(
        self,
        conversation: Conversation,
        task: AgentTask,
        node: AgentExecutionNode,
        call: ToolCall,
        run: InvocationRun,
        budget: BudgetAccount,
    ) -> None:
        exhausted: BudgetExhausted | None = None
        previous = task.model_copy(deep=True)
        previous_budget = budget.state.model_copy(deep=True)
        previous_stamp = conversation.updated_at
        try:
            with self.store.transaction():
                accept_verified_run(task, node, call, run)
                receipt = OutputArtifact(
                    **describe_evidence(
                        self._directory(conversation, task), agent_receipt_name(run.id)
                    ).model_dump()
                )
                for artifact in [*run.artifacts, *run.output.artifacts, receipt]:
                    if artifact not in task.artifacts:
                        task.artifacts.append(artifact)
                try:
                    self._progress(task, run, budget)
                except BudgetExhausted as exc:
                    exhausted = exc
                self.store.save(conversation)
        except BaseException:
            original_node = next(item for item in previous.nodes if item.id == node.id)
            original_call = next(item for item in previous.calls if item.id == call.id)
            for name in AgentExecutionNode.model_fields:
                setattr(node, name, getattr(original_node, name))
            for name in ToolCall.model_fields:
                setattr(call, name, getattr(original_call, name))
            for name in AgentTask.model_fields:
                setattr(task, name, getattr(previous, name))
            task.nodes = [node if item.id == node.id else item for item in task.nodes]
            task.calls = [call if item.id == call.id else item for item in task.calls]
            budget.state = previous_budget
            conversation.updated_at = previous_stamp
            raise
        if exhausted is not None:
            raise exhausted

    def _recover(
        self,
        conversation: Conversation,
        task: AgentTask,
        budget: BudgetAccount,
        *,
        action: str,
        task_id: str,
        run_state: RunState,
    ) -> Conversation:
        conversation_id = conversation.id
        if conversation.queue_origin is None:
            raise ExecutionUncertain("Queued action has no recorded origin")
        queue_origin = conversation.queue_origin.model_copy(deep=True)
        call_ids = [
            call.id
            for call in task.calls
            if call.status in ("running", "interrupted")
        ]
        for call_id in call_ids:
            node_id: str | None = None
            node_tool: str | None = None
            spec: InvocationSpec | None = None
            definition: CapabilityDefinition | None = None
            scheduling: SchedulingMetadata | None = None
            operation: OperationReservation | None = None
            try:
                with self.locks.hold(conversation_id):
                    conversation = self.store.get(conversation_id)
                    task = self._task(conversation)
                    if conversation.status == "completed":
                        if task.id != task_id:
                            raise ExecutionUncertain(
                                "Completed action task identity changed"
                            )
                        return conversation
                    self._validate_action_reservation(
                        conversation,
                        task,
                        action=action,
                        task_id=task_id,
                        statuses=("running",),
                    )
                    call = next(item for item in task.calls if item.id == call_id)
                    if call.status not in ("running", "interrupted"):
                        continue
                    node = next(
                        (item for item in task.nodes if item.id == call.node_id),
                        None,
                    )
                    node_id = node.id if node is not None else None
                    if (
                        node is None
                        or not self._spec_matches(
                            task,
                            node,
                            call,
                            task.pending_specs.get(call.id),
                        )
                    ):
                        raise ValueError(
                            "Pending invocation differs from the confirmed scope"
                        )
                    spec = task.pending_specs[call.id]
                    budget = self._budget(conversation, task)
                    run = load_agent_run(
                        self._directory(conversation, task), call.id, spec
                    )
                    if run is None and call.id in task.reuse_sources:
                        if "project_snapshots" in spec.numerics:
                            verify_snapshot_lineage(
                                task.baseline,
                                _node(node),
                                self.snapshots,
                            )
                        source = self._source(
                            conversation,
                            spec,
                            task.reuse_sources[call.id],
                        )
                        if source is None:
                            raise ValueError("Original cached receipt is missing")
                        run = self._reuse(
                            conversation, task, node, call, spec, source
                        )
                    if run is not None:
                        if "project_snapshots" in spec.numerics:
                            verify_snapshot_lineage(
                                task.baseline,
                                _node(node),
                                self.snapshots,
                            )
                        self._accept(
                            conversation, task, node, call, run, budget
                        )
                        continue
                    self._live_node(task, node)
                    definition = self.tools.definition(node.tool)
                    scheduling = self.tools.scheduling(node.tool)
                    node_tool = node.tool
                    conversation.status = "queued"
                    task.stage = f"queued:{scheduling.lane}:{node.id}"
                    self.store.save(conversation)
                with self.scheduler.slot(definition, scheduling):
                    with self.locks.hold(conversation_id):
                        conversation = self.store.get(conversation_id)
                        task = self._task(conversation)
                        if conversation.status == "completed":
                            if task.id != task_id:
                                raise ExecutionUncertain(
                                    "Completed action task identity changed"
                                )
                            return conversation
                        self._validate_action_reservation(
                            conversation,
                            task,
                            action=action,
                            task_id=task_id,
                            statuses=("queued",),
                        )
                        call = next(item for item in task.calls if item.id == call_id)
                        node = next(
                            item for item in task.nodes if item.id == node_id
                        )
                        if (
                            call.status not in ("running", "interrupted")
                            or conversation.queue_origin != queue_origin
                            or node.tool != node_tool
                            or call.node_id != node.id
                            or not self._spec_matches(
                                task,
                                node,
                                call,
                                task.pending_specs.get(call.id),
                            )
                            or conversation.status != "queued"
                            or task.stage
                            != f"queued:{scheduling.lane}:{node.id}"
                        ):
                            raise ExecutionUncertain(
                                "Queued reconciliation changed before lane admission"
                            )
                        self._verify_saved(conversation, task)
                        self._live_node(task, node)
                        if not set(node.depends_on) <= {
                            item.id
                            for item in task.nodes
                            if item.status == "completed"
                        }:
                            raise ExecutionUncertain(
                                "Queued reconciliation dependencies changed"
                            )
                        if (
                            self.tools.definition(node_tool) != definition
                            or self.tools.scheduling(node_tool) != scheduling
                        ):
                            raise ExecutionUncertain(
                                "Capability definition or scheduling lane changed"
                            )
                        budget = self._budget(conversation, task)
                        conversation.status = "running"
                        task.stage = node.id
                        self.store.save(conversation)
                        operation = OperationReservation(
                            action=action,
                            task_id=task_id,
                            queue_origin=queue_origin,
                            stamp=conversation.updated_at,
                            node_id=node.id,
                            node_tool=node_tool,
                            call_id=call.id,
                            spec=spec,
                            definition=definition,
                            scheduling=scheduling,
                        )
                        run_state.operation = operation
                        context = build_context(
                            conversation,
                            task,
                            node,
                            call,
                            self._external_budget(
                                conversation,
                                task,
                                operation=operation,
                            ),
                            store=self.store,
                            locks=self.locks,
                            action=action,
                            queue_origin=queue_origin,
                            operation=operation,
                            snapshots=self.snapshots,
                        )
                        if "project_snapshots" in spec.numerics:
                            verify_snapshot_lineage(
                                task.baseline,
                                _node(node),
                                self.snapshots,
                            )
                    output = self.tools.reconcile(
                        node_tool,
                        spec.inputs,
                        context,
                        context.scope,
                    )
                    with self.locks.hold(conversation_id):
                        conversation = self.store.get(conversation_id)
                        task = self._task(conversation)
                        if node_id is None:
                            raise ExecutionUncertain(
                                "Reconciliation node identity was not reserved"
                            )
                        node, call = self._validate_call_reservation(
                            conversation,
                            task,
                            operation=operation,
                        )
                        budget = self._budget(conversation, task)
                        if output is None:
                            raise ExecutionUncertain(
                                "Original invocation state is unknown; do not redispatch"
                            )
                        _validate_outcome(task, node, output)
                        run = build_invocation_run(call, spec, output)
                        persist_agent_run(context.directory, run)
                        self._accept(
                            conversation, task, node, call, run, budget
                        )
                        run_state.operation = None
            except ReconciledFailure as exc:
                if operation is None:
                    raise ExecutionUncertain(
                        "Reconciliation identity was not reserved"
                    ) from exc
                self._record_external_failure(
                    conversation_id,
                    operation,
                    exc,
                    interrupted=False,
                )
                raise
            except BudgetExhausted as exc:
                with self.locks.hold(conversation_id):
                    current = self.store.get(conversation_id)
                    current_task = self._task_by_id(current, task_id)
                    current_call = (
                        next(
                            (
                                item
                                for item in current_task.calls
                                if item.id == call_id
                            ),
                            None,
                        )
                        if current_task is not None
                        else None
                    )
                    if (
                        current_call is not None
                        and current_call.status == "completed"
                    ):
                        if operation is not None:
                            run_state.operation = None
                        raise
                if operation is None:
                    raise ExecutionUncertain(
                        "Reconciliation identity was not reserved"
                    ) from exc
                self._record_external_failure(
                    conversation_id,
                    operation,
                    exc,
                    interrupted=True,
                )
                raise ExecutionUncertain(
                    f"Original invocation state remains unknown: {exc}"
                ) from exc
            except (OSError, ValueError, RuntimeError) as exc:
                if operation is None:
                    raise ExecutionUncertain(
                        "Reconciliation identity was not reserved"
                    ) from exc
                self._record_external_failure(
                    conversation_id,
                    operation,
                    exc,
                    interrupted=True,
                )
                raise ExecutionUncertain(
                    f"Original invocation state remains unknown: {exc}"
                ) from exc
        with self.locks.hold(conversation_id):
            return self.store.get(conversation_id)

    @staticmethod
    def _progress(task: AgentTask, run: InvocationRun, budget: BudgetAccount) -> None:
        if run.id in task.progress_calls:
            return
        task.progress_calls.append(run.id)
        budget.note_progress(
            new_evidence=bool(
                any(item.valid for item in run.output.observations)
                or run.output.findings
                or run.output.tables
                or run.output.artifacts
                or run.output.evaluation is not None
            )
        )

    def _budget(self, conversation: Conversation, task: AgentTask) -> BudgetAccount:
        def checkpoint(state: BudgetState) -> None:
            task.budget_state = state
            self.store.save(conversation)

        return BudgetAccount(task.baseline.budget, task.budget_state, checkpoint)

    def _external_budget(
        self,
        conversation: Conversation,
        task: AgentTask,
        *,
        operation: OperationReservation,
    ) -> BudgetAccount:
        conversation_id = conversation.id
        task_id = operation.task_id
        last_state = task.budget_state.model_copy(deep=True)

        def checkpoint(state: BudgetState) -> None:
            nonlocal last_state
            with self.locks.hold(conversation_id):
                current = self.store.get(conversation_id)
                current_task = current.task
                if current_task is None or current_task.id != task_id:
                    raise ExecutionUncertain(
                        "Budget task changed during the admitted action"
                    )
                self._validate_call_reservation(
                    current,
                    current_task,
                    operation=operation,
                )
                if current_task.budget_state != last_state:
                    raise ExecutionUncertain(
                        "Budget state changed during the admitted action"
                    )
                current_task.budget_state = state
                task.budget_state = state
                self.store.save(current)
                operation.stamp = current.updated_at
                last_state = state.model_copy(deep=True)

        return BudgetAccount(task.baseline.budget, task.budget_state, checkpoint)

    @staticmethod
    def _ready(task: AgentTask, node: AgentExecutionNode) -> bool:
        completed = {
            item.id for item in task.nodes if item.status == "completed"
        }
        return node.status == "pending" and set(node.depends_on) <= completed

    @staticmethod
    def _validate_action_reservation(
        conversation: Conversation,
        task: AgentTask,
        *,
        action: str,
        task_id: str,
        statuses: tuple[str, ...],
    ) -> None:
        if (
            conversation.status not in statuses
            or conversation.queued_action != action
            or task.id != task_id
            or conversation.queue_origin is None
            or not task.baseline.confirmed
        ):
            raise ExecutionUncertain("Queued action reservation changed")

    def _validate_call_reservation(
        self,
        conversation: Conversation,
        task: AgentTask,
        *,
        operation: OperationReservation,
    ) -> tuple[AgentExecutionNode, ToolCall]:
        self._validate_action_reservation(
            conversation,
            task,
            action=operation.action,
            task_id=operation.task_id,
            statuses=("running",),
        )
        node = next(
            (
                item
                for item in task.nodes
                if item.id == operation.node_id
            ),
            None,
        )
        call = next(
            (
                item
                for item in task.calls
                if item.id == operation.call_id
            ),
            None,
        )
        if (
            conversation.queue_origin != operation.queue_origin
            or conversation.updated_at != operation.stamp
            or node is None
            or call is None
            or node.tool != operation.node_tool
            or call.node_id != operation.node_id
            or call.status not in ("running", "interrupted")
            or task.stage != operation.node_id
            or task.pending_specs.get(operation.call_id)
            != operation.spec
            or not self._spec_matches(task, node, call, operation.spec)
        ):
            raise ExecutionUncertain("Admitted call reservation changed")
        self._verify_saved(conversation, task)
        self._live_node(task, node)
        if not set(node.depends_on) <= {
            item.id
            for item in task.nodes
            if item.status == "completed"
        }:
            raise ExecutionUncertain(
                "Admitted call dependencies changed"
            )
        if (
            self.tools.definition(operation.node_tool)
            != operation.definition
            or self.tools.scheduling(operation.node_tool)
            != operation.scheduling
        ):
            raise ExecutionUncertain(
                "Capability definition or scheduling lane changed"
            )
        return node, call

    def _validate_recorded_failure(
        self,
        conversation: Conversation,
        task: AgentTask,
        operation: OperationReservation,
    ) -> None:
        self._validate_action_reservation(
            conversation,
            task,
            action=operation.action,
            task_id=operation.task_id,
            statuses=("running",),
        )
        node = next(
            (
                item
                for item in task.nodes
                if item.id == operation.node_id
            ),
            None,
        )
        call = next(
            (
                item
                for item in task.calls
                if item.id == operation.call_id
            ),
            None,
        )
        if (
            operation.failure_status not in ("failed", "interrupted")
            or operation.failure_error is None
            or conversation.queue_origin != operation.queue_origin
            or conversation.updated_at != operation.stamp
            or node is None
            or call is None
            or node.tool != operation.node_tool
            or call.node_id != operation.node_id
            or call.status != operation.failure_status
            or node.status != operation.failure_status
            or call.error != operation.failure_error
            or node.error != operation.failure_error
            or task.stage != operation.node_id
            or not self._spec_matches(task, node, call, operation.spec)
        ):
            raise ExecutionUncertain(
                "Recorded external failure reservation changed"
            )
        if operation.failure_status == "interrupted":
            if (
                task.pending_specs.get(operation.call_id)
                != operation.spec
            ):
                raise ExecutionUncertain(
                    "Interrupted invocation specification changed"
                )
        elif operation.call_id in task.pending_specs:
            raise ExecutionUncertain(
                "Failed invocation retained a pending specification"
            )
        self._verify_saved(conversation, task)
        self._live_node(task, node)
        if not set(node.depends_on) <= {
            item.id
            for item in task.nodes
            if item.status == "completed"
        }:
            raise ExecutionUncertain(
                "Recorded failure dependencies changed"
            )
        if (
            self.tools.definition(operation.node_tool)
            != operation.definition
            or self.tools.scheduling(operation.node_tool)
            != operation.scheduling
        ):
            raise ExecutionUncertain(
                "Recorded failure capability reservation changed"
            )

    def _dispatch(
        self,
        conversation: Conversation,
        task: AgentTask,
        node: AgentExecutionNode,
        budget: BudgetAccount,
        *,
        action: str,
        task_id: str,
        run_state: RunState,
    ) -> Conversation:
        conversation_id = conversation.id
        node_id = node.id
        node_tool = node.tool
        if conversation.queue_origin is None:
            raise ExecutionUncertain("Queued action has no recorded origin")
        queue_origin = conversation.queue_origin.model_copy(deep=True)
        with self.locks.hold(conversation_id):
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            self._validate_action_reservation(
                conversation,
                task,
                action=action,
                task_id=task_id,
                statuses=("running",),
            )
            node = next(item for item in task.nodes if item.id == node_id)
            if node.tool != node_tool or not self._ready(task, node):
                raise ExecutionUncertain(
                    "Confirmed node identity or dependency readiness changed"
                )
            self._verify_saved(conversation, task)
            self._live_node(task, node)
            definition = self.tools.definition(node.tool)
            scheduling = self.tools.scheduling(node.tool)
            conversation.status = "queued"
            node.status = "queued"
            task.stage = f"queued:{scheduling.lane}:{node.id}"
            self.store.save(conversation)
            timeout = budget.remaining_seconds()
        try:
            with self.scheduler.slot(definition, scheduling, timeout=timeout):
                with self.locks.hold(conversation_id):
                    conversation = self.store.get(conversation_id)
                    task = self._task(conversation)
                    self._validate_action_reservation(
                        conversation,
                        task,
                        action=action,
                        task_id=task_id,
                        statuses=("queued",),
                    )
                    node = next(item for item in task.nodes if item.id == node_id)
                    if (
                        conversation.queue_origin != queue_origin
                        or node.tool != node_tool
                        or node.status != "queued"
                        or task.stage != f"queued:{scheduling.lane}:{node.id}"
                    ):
                        raise ExecutionUncertain(
                            "Queued execution changed before lane admission"
                        )
                    node.status = "pending"
                    if not self._ready(task, node):
                        raise ExecutionUncertain(
                            "Queued node dependencies changed before lane admission"
                        )
                    self._verify_saved(conversation, task)
                    self._live_node(task, node)
                    if (
                        self.tools.definition(node_tool) != definition
                        or self.tools.scheduling(node_tool) != scheduling
                    ):
                        raise ExecutionUncertain(
                            "Capability definition or scheduling lane changed"
                        )
                    ancestors = plan_ancestors(task.baseline.nodes)[node.id] | {
                        node.id
                    }
                    local_ids = set(local_projects(task.baseline))
                    needs_snapshot = any(
                        local_ids.intersection(
                            candidate.resource_bindings.values()
                        )
                        for candidate in task.baseline.nodes
                        if candidate.id in ancestors
                    )
                    if needs_snapshot:
                        validate_snapshot_bindings(
                            task.baseline,
                            require=True,
                        )
                    verify_snapshot_lineage(
                        task.baseline,
                        _node(node),
                        self.snapshots,
                    )
                    verify_execution_resources(
                        task.baseline,
                        self.resources,
                    )
                    self._validate_resource_boundary(
                        task.baseline,
                        task.output_root or task.result_directory,
                    )
                    budget = self._budget(conversation, task)
                    budget.check_limits()
                    conversation.status = "running"
                    task.stage = node.id
                    self.store.save(conversation)
                    inputs = self.tools.validate_inputs(
                        node.tool,
                        resolve_inputs(_node(node), task.outputs),
                    )
                    spec = build_invocation_spec(
                        task.baseline, _node(node), inputs, task.outputs
                    )
                    source = (
                        self._source(conversation, spec)
                        if task.baseline.capability_snapshots[
                            node.tool
                        ].metadata.cacheable
                        else None
                    )
                    budget.charge_round()
                    budget.charge_call()
                    call = persist_running_call(
                        conversation, task, node, spec, store=self.store
                    )
                    if source is not None:
                        run = self._reuse(
                            conversation, task, node, call, spec, source
                        )
                        self._accept(
                            conversation, task, node, call, run, budget
                        )
                        if all(
                            item.status == "completed" for item in task.nodes
                        ):
                            self._finish(conversation, task)
                            self.store.save(conversation)
                        return conversation
                    operation = OperationReservation(
                        action=action,
                        task_id=task_id,
                        queue_origin=queue_origin,
                        stamp=conversation.updated_at,
                        node_id=node.id,
                        node_tool=node_tool,
                        call_id=call.id,
                        spec=spec,
                        definition=definition,
                        scheduling=scheduling,
                    )
                    run_state.operation = operation
                    context = build_context(
                        conversation,
                        task,
                        node,
                        call,
                        self._external_budget(
                            conversation,
                            task,
                            operation=operation,
                        ),
                        store=self.store,
                        locks=self.locks,
                        action=action,
                        queue_origin=queue_origin,
                        operation=operation,
                        snapshots=self.snapshots,
                    )
                    call_id = call.id
                try:
                    output = self.tools.invoke(
                        node_tool, inputs, context, context.scope
                    )
                except ExecutionUncertain as exc:
                    self._record_external_failure(
                        conversation_id,
                        operation,
                        exc,
                        interrupted=True,
                    )
                    raise
                except BudgetExhausted as exc:
                    pre_dispatch = isinstance(
                        exc, PreDispatchBudgetExhausted
                    )
                    self._record_external_failure(
                        conversation_id,
                        operation,
                        exc,
                        interrupted=not pre_dispatch,
                    )
                    if pre_dispatch:
                        raise
                    raise ExecutionUncertain(
                        f"Original invocation state remains unknown: {exc}"
                    ) from exc
                except (OSError, ValueError, RuntimeError) as exc:
                    self._record_external_failure(
                        conversation_id,
                        operation,
                        exc,
                        interrupted=False,
                    )
                    raise
                with self.locks.hold(conversation_id):
                    conversation = self.store.get(conversation_id)
                    task = self._task(conversation)
                    node, call = self._validate_call_reservation(
                        conversation,
                        task,
                        operation=operation,
                    )
                    if call.status != "running":
                        raise ExecutionUncertain(
                            "Invocation state changed before its result was applied"
                        )
                    budget = self._budget(conversation, task)
                    publishing = False
                    try:
                        _validate_outcome(task, node, output)
                        run = build_invocation_run(call, spec, output)
                        publishing = True
                        persist_agent_run(
                            self._directory(conversation, task), run
                        )
                        self._accept(
                            conversation, task, node, call, run, budget
                        )
                        run_state.operation = None
                    except BudgetExhausted as exc:
                        node = next(
                            item for item in task.nodes if item.id == node_id
                        )
                        call = next(
                            item for item in task.calls if item.id == call_id
                        )
                        if call.status == "completed":
                            run_state.operation = None
                            raise
                        call.status = "interrupted"
                        call.error = str(exc)[:4000]
                        node.status = "interrupted"
                        node.error = call.error
                        self.store.save(conversation)
                        operation.stamp = conversation.updated_at
                        operation.failure_status = call.status
                        operation.failure_error = call.error
                        raise ExecutionUncertain(
                            "Original receipt publication or acceptance "
                            f"is uncertain: {exc}"
                        ) from exc
                    except (OSError, ValueError, RuntimeError) as exc:
                        node = next(
                            item for item in task.nodes if item.id == node_id
                        )
                        call = next(
                            item for item in task.calls if item.id == call_id
                        )
                        if publishing:
                            call.status = "interrupted"
                            call.error = str(exc)[:4000]
                            node.status = "interrupted"
                            node.error = call.error
                            task.pending_specs[call.id] = spec
                            task.outputs.pop(node.id, None)
                            task.analysis_runs = [
                                run
                                for run in task.analysis_runs
                                if run.id != call.id
                            ]
                            self.store.save(conversation)
                            operation.stamp = conversation.updated_at
                            operation.failure_status = call.status
                            operation.failure_error = call.error
                            raise ExecutionUncertain(
                                "Original receipt publication or acceptance "
                                f"is uncertain: {exc}"
                            ) from exc
                        call.status = "failed"
                        call.error = str(exc)[:4000]
                        call.finished_at = now()
                        node.status = "failed"
                        node.error = call.error
                        task.pending_specs.pop(call.id, None)
                        self.store.save(conversation)
                        operation.stamp = conversation.updated_at
                        operation.failure_status = call.status
                        operation.failure_error = call.error
                        raise
                    if all(
                        item.status == "completed" for item in task.nodes
                    ):
                        self._finish(conversation, task)
                        self.store.save(conversation)
                    return conversation
        finally:
            with self.locks.hold(conversation_id):
                current = self.store.get(conversation_id)
                current_task = self._task_by_id(current, task_id)
                current_node = (
                    next(
                        (
                            item
                            for item in current_task.nodes
                            if item.id == node_id
                        ),
                        None,
                    )
                    if current_task is not None
                    else None
                )
                if current_node is not None and current_node.status == "queued":
                    current_node.status = "pending"
                    self.store.save(current)

    def _record_external_failure(
        self,
        conversation_id: str,
        operation: OperationReservation,
        error: BaseException,
        *,
        interrupted: bool,
    ) -> None:
        with self.locks.hold(conversation_id):
            conversation = self.store.get(conversation_id)
            task = conversation.task
            if task is None:
                raise ExecutionUncertain(
                    "Invocation task changed while the capability was running"
                ) from error
            try:
                node, call = self._validate_call_reservation(
                    conversation,
                    task,
                    operation=operation,
                )
            except ExecutionUncertain as stale:
                raise stale from error
            call.status = "interrupted" if interrupted else "failed"
            call.error = str(error)[:4000]
            node.status = "interrupted" if interrupted else "failed"
            node.error = call.error
            if not interrupted:
                call.finished_at = now()
                task.pending_specs.pop(call.id, None)
            self.store.save(conversation)
            operation.stamp = conversation.updated_at
            operation.failure_status = call.status
            operation.failure_error = call.error

    @staticmethod
    def _result(task: AgentTask, status: str) -> TaskResult:
        output = combine_outcomes(task.outputs)
        gaps = list(task.baseline.gaps)
        gap_ids = {gap.requirement_id for gap in gaps}
        for missing in output.missing_outputs(task.baseline.requirements):
            if missing.id not in gap_ids:
                gaps.append(
                    CapabilityGap(
                        requirement_id=missing.id,
                        limitation="Requested output was not produced by the confirmed execution.",
                        next_step="Review the recorded failure or propose a new confirmed plan.",
                    )
                )
        result = TaskResult.model_validate(
            {
                **output.model_dump(mode="json"),
                "execution_status": "partial" if status == "completed" and gaps else status,
                "unmet_requirements": [gap.model_dump(mode="json") for gap in gaps],
            }
        )
        result.validate_requirements(task.baseline.requirements)
        return result

    def _finish(
        self, conversation: Conversation, task: AgentTask
    ) -> None:
        self._verify_saved(conversation, task)
        task.result = self._result(task, "completed")
        task.stage = task.result.execution_status
        conversation.status = "completed"
        conversation.queued_action = None
        conversation.queued_action_committed = False
        conversation.queue_origin = None
        conversation.messages.append(
            ConversationMessage(
                role="assistant",
                content=(
                    "已记录执行结果，仍有未满足需求，请查看限制与证据。"
                    if task.result.unmet_requirements
                    else "已完成确认范围内的执行，结果与证据已记录。"
                ),
                created_at=now(),
            )
        )

    def _run(
        self,
        conversation_id: str,
        action: str,
        task_id: str,
    ) -> Conversation:
        verified = False
        reservation_origin: ConversationState | None = None
        run_state = RunState()
        try:
            with self.locks.hold(conversation_id):
                conversation = self.store.get(conversation_id)
                task = self._task(conversation)
                if (
                    conversation.status != "queued"
                    or conversation.queued_action != action
                    or task.id != task_id
                    or conversation.queue_origin is None
                ):
                    raise ExecutionUncertain(
                        "Queued action changed before execution started"
                    )
                reservation_origin = conversation.queue_origin.model_copy(
                    deep=True
                )
                self._validate_structure(task)
                self._directory(conversation, task)
                conversation.turn_origin = None
                conversation.status = "running"
                conversation.pending_decision = None
                conversation.error = None
                self.store.save(conversation)
                self._verify_saved(conversation, task)
                verified = True
                budget = self._budget(conversation, task)
                for run in task.analysis_runs:
                    self._progress(task, run, budget)
            conversation = self._recover(
                conversation,
                task,
                budget,
                action=action,
                task_id=task_id,
                run_state=run_state,
            )
            with self.locks.hold(conversation_id):
                conversation = self.store.get(conversation_id)
                task = self._task(conversation)
                self._validate_action_reservation(
                    conversation,
                    task,
                    action=action,
                    task_id=task_id,
                    statuses=("running",),
                )
                budget = self._budget(conversation, task)
                if (
                    task.result is not None
                    and task.result.execution_status == "partial"
                ):
                    budget.check_limits()
                if any(node.status == "failed" for node in task.nodes):
                    raise ValueError(
                        "A known failed call requires an explicit retry"
                    )
                scope = _scope(task.baseline)
                max_steps = task.baseline.budget.max_calls

            next_node_id: str | None = None

            def decide_next() -> LoopDecision:
                nonlocal next_node_id
                with self.locks.hold(conversation_id):
                    conversation = self.store.get(conversation_id)
                    task = self._task(conversation)
                    if conversation.status == "completed":
                        if task.id != task_id:
                            raise ExecutionUncertain(
                                "Completed action task identity changed"
                            )
                        return LoopDecision(
                            action="stop",
                            reason="Confirmed execution is complete",
                        )
                    self._validate_action_reservation(
                        conversation,
                        task,
                        action=action,
                        task_id=task_id,
                        statuses=("running",),
                    )
                    self._validate_structure(task)
                    completed = {
                        node.id
                        for node in task.nodes
                        if node.status == "completed"
                    }
                    node = next(
                        (
                            item
                            for item in task.nodes
                            if item.status == "pending"
                            and set(item.depends_on) <= completed
                        ),
                        None,
                    )
                    if all(
                        item.status == "completed" for item in task.nodes
                    ):
                        next_node_id = None
                        return LoopDecision(
                            action="stop",
                            reason="All confirmed nodes are complete",
                        )
                    if node is None:
                        raise ExecutionUncertain(
                            "No safe pending node is ready; reconcile unknown calls"
                        )
                    next_node_id = node.id
                    return LoopDecision(
                        action="continue",
                        effect=task.baseline.capability_snapshots[
                            node.tool
                        ].effect,
                        reason=f"Execute the confirmed ready node: {node.id}",
                    )

            def execute_next() -> None:
                nonlocal verified
                with self.locks.hold(conversation_id):
                    conversation = self.store.get(conversation_id)
                    task = self._task(conversation)
                    self._validate_action_reservation(
                        conversation,
                        task,
                        action=action,
                        task_id=task_id,
                        statuses=("running",),
                    )
                    node = next(
                        (
                            item
                            for item in task.nodes
                            if item.id == next_node_id
                        ),
                        None,
                    )
                    if node is None or not self._ready(task, node):
                        raise ExecutionUncertain(
                            "Selected node changed before dispatch"
                        )
                    self._verify_saved(conversation, task)
                    verified = True
                    budget = self._budget(conversation, task)
                self._dispatch(
                    conversation,
                    task,
                    node,
                    budget,
                    action=action,
                    task_id=task_id,
                    run_state=run_state,
                )

            run_loop(
                decide=decide_next,
                execute=execute_next,
                persist=lambda: None,
                scope=scope,
                max_steps=max_steps,
            )

            with self.locks.hold(conversation_id):
                conversation = self.store.get(conversation_id)
                task = self._task(conversation)
                if conversation.status == "completed":
                    if task.id != task_id:
                        raise ExecutionUncertain(
                            "Completed action task identity changed"
                        )
                    return conversation
                self._validate_action_reservation(
                    conversation,
                    task,
                    action=action,
                    task_id=task_id,
                    statuses=("running",),
                )
                verified = False
                self._finish(conversation, task)
                verified = True
                self.store.save(conversation)
                return conversation
        except (OSError, ValueError, RuntimeError) as exc:
            logger.exception("Agent execution stopped for conversation %s", conversation_id)
            with self.locks.hold(conversation_id):
                conversation = self.store.get(conversation_id)
                failed_task = conversation.task
                if (
                    reservation_origin is None
                    or failed_task is None
                    or failed_task.id != task_id
                    or conversation.status not in ("queued", "running")
                    or conversation.queued_action != action
                    or conversation.queue_origin != reservation_origin
                ):
                    return conversation
                operation = run_state.operation
                if operation is not None:
                    try:
                        if operation.failure_status is None:
                            self._validate_call_reservation(
                                conversation,
                                failed_task,
                                operation=operation,
                            )
                        else:
                            self._validate_recorded_failure(
                                conversation,
                                failed_task,
                                operation,
                            )
                    except (OSError, ValueError, RuntimeError):
                        return conversation
                uncertain = isinstance(exc, ExecutionUncertain)
                conversation.status = (
                    "interrupted" if uncertain else "failed"
                )
                conversation.error = str(exc)[:4000]
                if failed_task is not None:
                    failed_task.stage = "interrupted" if uncertain else "failed"
                    failed_task.result = (
                        self._result(
                            failed_task,
                            "interrupted"
                            if uncertain
                            else "partial"
                            if isinstance(exc, BudgetExhausted)
                            else "failed",
                        )
                        if verified
                        else None
                    )
                conversation.pending_decision = PendingDecision(
                    kind=(
                        "resume"
                        if uncertain
                        else "clarify"
                        if isinstance(exc, BudgetExhausted)
                        else "retry"
                    ),
                    message=(
                        conversation.error
                        + " Submit a new plan with a revised budget."
                        if isinstance(exc, BudgetExhausted)
                        else conversation.error
                    ),
                )
                conversation.queued_action = None
                conversation.queued_action_committed = False
                conversation.queue_origin = None
                self.store.save(conversation)
                return conversation
