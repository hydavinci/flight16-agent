import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from ..jobs import BusyError
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
from ..runtime.capabilities import CapabilityRegistry, validate_declared_outcome
from ..runtime.contracts import fingerprint
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
from ..runtime.requirements import CapabilityGap
from ..runtime.resources import ResourceReference, bounded_json
from .compat import has_unresolved_call
from .contracts import (
    AgentExecutionNode,
    AgentTask,
    Conversation,
    ConversationMessage,
    Decision,
    PendingDecision,
    ToolCall,
)
from .intake import safe_directory, validate_output_root
from .resources import ResourceRegistry
from .store import ConversationStore, now

logger = logging.getLogger(__name__)
MAX_CHECKPOINT_BYTES = 4_000_000


class ExecutionUncertain(RuntimeError):
    """A dispatched operation has no proven terminal state."""


class ReconciledFailure(RuntimeError):
    """A reconciliation handler has verified the original operation failed."""


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

    def resource(self, role: str) -> ResourceReference:
        if role not in self._resource_roles:
            raise ValueError(f"Resource role is outside this node's bindings: {role}")
        return ResourceReference.model_validate(self._resource_roles[role])


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
    store: ConversationStore,
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
    store: ConversationStore,
) -> AgentContext:
    directory = store.task_directory(conversation.id, task.id, task.output_root)
    if str(directory) != task.result_directory:
        raise ValueError("Task result directory does not match its persisted identity")
    spec = task.pending_specs[call.id]
    name = f"agent/checkpoints/{call.id}/checkpoint.json"

    def active() -> None:
        if call.status not in ("running", "interrupted") or task.pending_specs.get(call.id) != spec:
            raise ValueError("Checkpoint access is outside the original unresolved call")
        if call.id in task.checkpoints and task.checkpoints[call.id] != name:
            raise ValueError("Checkpoint reference is outside this call's directory")

    def load_checkpoint() -> dict[str, JsonValue] | None:
        active()
        if call.id not in task.checkpoints:
            return None
        descriptor = describe_evidence(directory, name)
        if descriptor.sha256 != task.checkpoint_sha256.get(call.id):
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
            or payload.get("call_id") != call.id
            or payload.get("spec_fingerprint") != spec.fingerprint
            or not isinstance(payload.get("data"), dict)
        ):
            raise ValueError("Checkpoint identity does not match the original call")
        bounded_json(payload)
        fingerprint(payload)
        return payload["data"]

    def save_checkpoint(data: dict[str, JsonValue]) -> None:
        active()
        if not isinstance(data, dict):
            raise ValueError("Checkpoint data must be a JSON object")
        payload = {"call_id": call.id, "spec_fingerprint": spec.fingerprint, "data": data}
        bounded_json(payload)
        fingerprint(payload)
        content = (
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(content) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds its serialized size limit")
        path = directory / name
        safe_directory(path.parent)
        if path.is_symlink():
            raise ValueError("Unsafe checkpoint symlink")
        if path.exists():
            if call.id not in task.checkpoints:
                raise ValueError("Unrecorded checkpoint cannot replace current state")
            load_checkpoint()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".checkpoint-", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        descriptor = describe_evidence(directory, name)
        task.checkpoints[call.id] = name
        task.checkpoint_sha256[call.id] = descriptor.sha256
        store.save(conversation)

    resources = {resource.id: resource for resource in task.baseline.resources}
    roles = {
        role: ResourceReference.model_validate(resources[identifier])
        for role, identifier in node.resource_bindings.items()
    }
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
    )


class AgentExecutor:
    def __init__(
        self,
        store: ConversationStore,
        tools: CapabilityRegistry[AgentContext],
        resources: ResourceRegistry,
    ):
        self.store = store
        self.tools = tools
        self.resources = resources
        self.lock = threading.Lock()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self.lock.acquire(blocking=False):
            raise BusyError("Another agent operation is active")
        try:
            yield
        finally:
            self.lock.release()

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
    def _validate_structure(task: AgentTask) -> None:
        baseline = AgentBaseline.model_validate(task.baseline)
        if [_node(node) for node in task.nodes] != baseline.nodes:
            raise ValueError("Execution nodes differ from the confirmed baseline")
        if set(baseline.capability_snapshots) != {node.tool for node in baseline.nodes}:
            raise ValueError("Baseline snapshots must cover exactly the confirmed capabilities")
        if baseline.confirmed and (
            task.baseline_sha256 != fingerprint(baseline.model_dump(mode="json"))
        ):
            raise ValueError("Baseline differs from its recorded confirmation fingerprint")

    def _validate_resource_boundary(self, baseline: AgentPlan, output: str | None) -> None:
        validate_output_root(output)
        for source in self.resources.local_roots(baseline.resources):
            validate_output_root(output, str(source))

    def propose(self, conversation: Conversation, plan: AgentPlan) -> Conversation:
        with self._operation():
            tasks = [
                *conversation.archived_tasks,
                *([conversation.task] if conversation.task else []),
            ]
            if any(has_unresolved_call(task) for task in tasks):
                raise ValueError(
                    "Reconcile unresolved or unknown calls before proposing a new plan"
                )
            plan = AgentPlan.model_validate(plan)
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
            self._validate_resource_boundary(plan, effective)
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
            if conversation.task is not None:
                conversation.archived_tasks.append(conversation.task)
            conversation.task = task
            conversation.output_root = output_root
            conversation.turn_origin = None
            conversation.active_workflow = None
            conversation.workflow_agenda = []
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

    def confirm(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            if trusted is not True or type(version) is not int or version != task.baseline.version:
                raise ValueError(
                    "Explicit consent for the exact current baseline version is required"
                )
            self._validate_structure(task)
            if task.baseline.confirmed:
                if conversation.status == "completed":
                    return conversation
                raise ValueError("The baseline is already confirmed; resume the original task")
            validate_bindings(task.baseline, self.tools)
            self.resources.verify(task.baseline.resources)
            self._validate_resource_boundary(task.baseline, task.result_directory)
            self._directory(conversation, task, create=True)
            task.baseline = AgentBaseline.model_validate(
                {
                    **task.baseline.model_dump(),
                    "confirmed": True,
                    "confirmed_at": now(),
                }
            )
            task.baseline_sha256 = fingerprint(task.baseline.model_dump(mode="json"))
            task.decisions.append(
                Decision(
                    action="confirm_baseline",
                    baseline_version=version,
                    trusted=True,
                    created_at=now(),
                )
            )
            self.store.save(conversation)
            return self._run(conversation, task)

    def resume(self, conversation_id: str) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            if not task.baseline.confirmed:
                raise ValueError("Execution requires a confirmed baseline")
            self._validate_structure(task)
            task.decisions.append(
                Decision(action="resume", baseline_version=task.baseline.version, created_at=now())
            )
            return self._run(conversation, task)

    def retry(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            if has_unresolved_call(task):
                raise ValueError("Reconcile unknown or unresolved calls before retrying")
            if trusted is not True or type(version) is not int or version != task.baseline.version:
                raise ValueError("Retry requires explicit consent for the current baseline version")
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
            self.store.save(conversation)
            return self._run(conversation, task)

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
            spec = self._spec(task, node, call)
            if (
                call.inputs != spec.inputs
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
                if self._spec(task, node, call) != spec:
                    raise ValueError("Cached receipt differs from its original confirmed scope")
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
    ) -> None:
        accept_verified_run(task, node, call, run)
        receipt = OutputArtifact(
            **describe_evidence(
                self._directory(conversation, task), agent_receipt_name(run.id)
            ).model_dump()
        )
        for artifact in [*run.artifacts, *run.output.artifacts, receipt]:
            if artifact not in task.artifacts:
                task.artifacts.append(artifact)
        self.store.save(conversation)

    def _recover(self, conversation: Conversation, task: AgentTask, budget: BudgetAccount) -> None:
        for call in task.calls:
            if call.status not in ("running", "interrupted"):
                continue
            node = next((node for node in task.nodes if node.id == call.node_id), None)
            try:
                if node is None or task.pending_specs.get(call.id) != self._spec(task, node, call):
                    raise ValueError("Pending invocation differs from the confirmed scope")
                spec = task.pending_specs[call.id]
                run = load_agent_run(self._directory(conversation, task), call.id, spec)
                if run is None and call.id in task.reuse_sources:
                    source = self._source(conversation, spec, task.reuse_sources[call.id])
                    if source is None:
                        raise ValueError("Original cached receipt is missing")
                    run = self._reuse(conversation, task, node, call, spec, source)
                if run is None:
                    self._live_node(task, node)
                    context = build_context(
                        conversation, task, node, call, budget, store=self.store
                    )
                    output = self.tools.reconcile(node.tool, spec.inputs, context, context.scope)
                    if output is None:
                        raise ExecutionUncertain(
                            "Original invocation state is unknown; do not redispatch"
                        )
                    _validate_outcome(task, node, output)
                    run = build_invocation_run(call, spec, output)
                    persist_agent_run(context.directory, run)
                self._accept(conversation, task, node, call, run)
            except ReconciledFailure as exc:
                call.status = "failed"
                call.error = str(exc)[:4000]
                call.finished_at = now()
                if node is not None:
                    node.status = "failed"
                    node.error = call.error
                task.pending_specs.pop(call.id, None)
                self.store.save(conversation)
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                call.status = "interrupted"
                call.error = str(exc)[:4000]
                if node is not None:
                    node.status = "interrupted"
                    node.error = call.error
                self.store.save(conversation)
                raise ExecutionUncertain(
                    f"Original invocation state remains unknown: {exc}"
                ) from exc
            self._progress(task, run, budget)

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

    def _dispatch(
        self,
        conversation: Conversation,
        task: AgentTask,
        node: AgentExecutionNode,
        budget: BudgetAccount,
    ) -> None:
        self._live_node(task, node)
        self.resources.verify(task.baseline.resources)
        self._validate_resource_boundary(task.baseline, task.result_directory)
        inputs = self.tools.validate_inputs(node.tool, resolve_inputs(_node(node), task.outputs))
        spec = build_invocation_spec(task.baseline, _node(node), inputs, task.outputs)
        source = (
            self._source(conversation, spec)
            if task.baseline.capability_snapshots[node.tool].metadata.cacheable
            else None
        )
        budget.charge_round()
        budget.charge_call()
        call = persist_running_call(conversation, task, node, spec, store=self.store)
        publishing = False
        try:
            if source is not None:
                publishing = True
                run = self._reuse(conversation, task, node, call, spec, source)
            else:
                context = build_context(conversation, task, node, call, budget, store=self.store)
                output = self.tools.invoke(node.tool, inputs, context, context.scope)
                _validate_outcome(task, node, output)
                run = build_invocation_run(call, spec, output)
                publishing = True
                persist_agent_run(context.directory, run)
            self._accept(conversation, task, node, call, run)
        except ExecutionUncertain as exc:
            call.status = "interrupted"
            call.error = str(exc)[:4000]
            node.status = "interrupted"
            node.error = call.error
            self.store.save(conversation)
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            if publishing:
                call.status = "interrupted"
                call.error = str(exc)[:4000]
                node.status = "interrupted"
                node.error = call.error
                task.pending_specs[call.id] = spec
                task.outputs.pop(node.id, None)
                task.analysis_runs = [run for run in task.analysis_runs if run.id != call.id]
                self.store.save(conversation)
                raise ExecutionUncertain(
                    f"Original receipt publication or acceptance is uncertain: {exc}"
                ) from exc
            call.status = "failed"
            call.error = str(exc)[:4000]
            call.finished_at = now()
            node.status = "failed"
            node.error = call.error
            task.pending_specs.pop(call.id, None)
            self.store.save(conversation)
            raise
        self._progress(task, run, budget)

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

    def _run(self, conversation: Conversation, task: AgentTask) -> Conversation:
        self._validate_structure(task)
        self._directory(conversation, task)
        conversation.turn_origin = None
        conversation.status = "running"
        conversation.pending_decision = None
        conversation.error = None
        self.store.save(conversation)
        verified = False

        def checkpoint(state: BudgetState) -> None:
            task.budget_state = state
            self.store.save(conversation)

        try:
            self._verify_saved(conversation, task)
            verified = True
            budget = BudgetAccount(task.baseline.budget, task.budget_state, checkpoint)
            for run in task.analysis_runs:
                self._progress(task, run, budget)
            self._recover(conversation, task, budget)
            if task.result is not None and task.result.execution_status == "partial":
                budget.check_limits()
            if any(node.status == "failed" for node in task.nodes):
                raise ValueError("A known failed call requires an explicit retry")

            def ready() -> AgentExecutionNode | None:
                completed = {node.id for node in task.nodes if node.status == "completed"}
                return next(
                    (
                        node
                        for node in task.nodes
                        if node.status == "pending" and set(node.depends_on) <= completed
                    ),
                    None,
                )

            def decide() -> LoopDecision:
                if all(node.status == "completed" for node in task.nodes):
                    return LoopDecision(action="stop", reason="Every confirmed node is complete")
                node = ready()
                if node is None:
                    raise ExecutionUncertain(
                        "No safe pending node is ready; reconcile unknown calls"
                    )
                return LoopDecision(
                    action="continue",
                    effect=task.baseline.capability_snapshots[node.tool].effect,
                    reason=f"Execute the confirmed ready node: {node.id}",
                )

            def execute() -> None:
                nonlocal verified
                verified = False
                self._verify_saved(conversation, task)
                verified = True
                node = ready()
                if node is None:
                    raise ExecutionUncertain("No confirmed pending node remains")
                self._dispatch(conversation, task, node, budget)

            run_loop(
                decide=decide,
                execute=execute,
                persist=lambda: self.store.save(conversation),
                scope=_scope(task.baseline),
                max_steps=task.baseline.budget.max_calls,
            )
            verified = False
            self._verify_saved(conversation, task)
            verified = True
            task.result = self._result(task, "completed")
            task.stage = task.result.execution_status
            conversation.status = "completed"
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
        except (OSError, ValueError, RuntimeError) as exc:
            logger.exception("Agent execution stopped for %s", task.id)
            uncertain = isinstance(exc, ExecutionUncertain)
            conversation.status = "interrupted" if uncertain else "failed"
            conversation.error = str(exc)[:4000]
            task.stage = "interrupted" if uncertain else "failed"
            task.result = (
                self._result(
                    task,
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
                kind="resume" if uncertain else "retry", message=conversation.error
            )
        self.store.save(conversation)
        return conversation
