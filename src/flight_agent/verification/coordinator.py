import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial

from ..jobs import BusyError
from ..runtime.resources import ResourceReference
from .agent_planner import AgentPlanner, bind_plan, message_sources
from .agent_runtime import AgentExecutor
from .compat import Action, has_unresolved_call, legacy_decision, resolve_location
from .contracts import (
    AgentTask,
    Conversation,
    ConversationMessage,
    ConversationState,
    PendingDecision,
)
from .intake import parse_directories, validate_output_root
from .resources import ResourceRegistry
from .runtime import VerificationRuntime
from .store import ConversationStore, now


class EngineeringRuntime:
    def __init__(
        self,
        store: ConversationStore,
        planner: AgentPlanner,
        executor: AgentExecutor,
        resources: ResourceRegistry,
        legacy: VerificationRuntime | None,
    ):
        if executor.store is not store or executor.resources is not resources:
            raise ValueError("Coordinator and executor must share the same store and resources")
        if legacy is not None and legacy.store is not store:
            raise ValueError("Legacy recovery must share the coordinator store")
        self.store, self.planner, self.executor = store, planner, executor
        self.resources, self.legacy = resources, legacy
        self.lock = threading.Lock()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self.lock.acquire(blocking=False):
            raise BusyError("Another engineering operation is running")
        try:
            yield
        finally:
            self.lock.release()

    @staticmethod
    def _message(record: Conversation, role: str, text: str) -> None:
        record.messages.append(
            ConversationMessage.model_validate(
                {
                    "role": role,
                    "content": text,
                    "created_at": now(),
                }
            )
        )

    def _wait(self, record: Conversation, text: str) -> Conversation:
        record.turn_origin = None
        record.status = "waiting_for_user"
        record.error = None
        record.pending_decision = PendingDecision(kind="clarify", message=text)
        self._message(record, "assistant", text)
        self.store.save(record)
        return record

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
        with self._operation():
            if not message.strip() or len(message) > 4000:
                raise ValueError("A message between 1 and 4000 characters is required")
            selected = resolve_location(location, scenario, self.resources.locations())
            record = self.store.get(conversation_id) if conversation_id else self.store.create()
            self._message(record, "user", message.strip())
            if record.task is not None and has_unresolved_call(record.task):
                self._message(
                    record,
                    "assistant",
                    "已有状态未知的调用；请先续接核对原运行，不得切换或重复派发。",
                )
                self.store.save(record)
                return record
            if record.status in ("planning", "running"):
                raise BusyError("The current conversation is already active")
            record.turn_origin = (
                ConversationState(
                    status=record.status,
                    pending_decision=record.pending_decision,
                    error=record.error,
                )
                if record.task is not None
                else None
            )
            changed = (
                selected is not None
                and record.resource_location is not None
                and selected != record.resource_location
            )
            intake = parse_directories(
                message,
                project_root,
                output_root,
                current_project=(
                    None
                    if changed or project_root is not None and not project_root.strip()
                    else record.project_root
                ),
                current_output=record.output_root,
            )
            if intake.question is not None:
                return self._wait(record, intake.question)
            record.project_root = intake.project_root
            record.output_root = validate_output_root(intake.output_root)
            record.output_root_reset = output_root is not None and not output_root.strip()
            record.resource_location = selected or record.resource_location
            if record.project_root and record.resource_location is None:
                locations = self.resources.locations()
                if len(locations) != 1:
                    return self._wait(
                        record, "请明确目录所属的已注册资源位置，不能猜测 Agent 或 Worker。"
                    )
                record.resource_location = locations[0].id
            record.status = "planning"
            record.pending_decision = None
            record.error = None
            self.store.save(record)
            return record

    def continue_submission(self, conversation_id: str) -> Conversation:
        with self._operation():
            record = self.store.get(conversation_id)
            if (
                record.status != "planning"
                or not record.messages
                or record.messages[-1].role != "user"
            ):
                raise ValueError("No queued user message is available for planning")
            resources: dict[str, ResourceReference] = {}
            if record.project_root:
                if record.resource_location is None:
                    return self._wait(record, "请先选择资源位置。")
                resource = self.resources.inspect(record.resource_location, record.project_root)
                resources[resource.id] = resource
            catalog = self.executor.tools.catalog(tuple(resources.values()))
            sources = message_sources(record)
            decision = self.planner.propose(
                record.messages[-1].content,
                record,
                catalog=catalog,
                resources=resources,
                sources=sources,
            )
            if decision.plan is not None:
                plan = bind_plan(decision.plan, resources, sources, catalog)
                return self.executor.propose(record, plan)
            record.messages.append(
                ConversationMessage(
                    role="assistant",
                    content=decision.text,
                    created_at=now(),
                    response_kind=decision.action,
                    gaps=decision.gaps,
                )
            )
            if decision.action == "answer":
                origin = record.turn_origin
                record.status = origin.status if origin is not None else "completed"
                record.pending_decision = origin.pending_decision if origin is not None else None
                record.error = origin.error if origin is not None else None
            else:
                record.status = "waiting_for_user"
                record.error = None
                record.pending_decision = PendingDecision(
                    kind="capability_gap" if decision.action == "capability_gap" else "clarify",
                    message=decision.text,
                )
            record.turn_origin = None
            self.store.save(record)
            return record

    def prepare_decision(
        self,
        conversation_id: str,
        action: Action,
        *,
        version: int | None = None,
        trusted: bool = False,
    ) -> Callable[[], Conversation] | None:
        record = self.store.get(conversation_id)
        task = record.task
        if task is None:
            raise ValueError("No task has been planned")
        if not isinstance(task, AgentTask):
            if self.legacy is None:
                raise ValueError("Legacy task recovery is unavailable")
            return legacy_decision(
                self.legacy,
                conversation_id,
                action,
                version=version,
                trusted=trusted,
            )
        if action != "resume" and (
            type(version) is not int or version != task.baseline.version or trusted is not True
        ):
            raise ValueError("Explicit consent for the current baseline version is required")
        if action in ("confirm", "resume") and record.status == "completed":
            return None
        if action == "confirm":
            if (
                task.baseline.confirmed
                or record.pending_decision is None
                or record.pending_decision.kind != "confirm_baseline"
            ):
                raise ValueError("A new unconfirmed baseline is required")
            return partial(
                self.executor.confirm, conversation_id, task.baseline.version, trusted=True
            )
        if not task.baseline.confirmed:
            raise ValueError("Confirm the current baseline before continuing")
        if record.pending_decision is not None and record.pending_decision.kind not in (
            "resume",
            "retry",
        ):
            raise ValueError("Address the current follow-up before continuing the previous task")
        if action == "resume":
            return partial(self.executor.resume, conversation_id)
        if action == "retry":
            if has_unresolved_call(task):
                raise ValueError("Reconcile unknown calls before retrying")
            if not any(node.status == "failed" for node in task.nodes):
                raise ValueError("Only a known failed invocation may be retried")
            return partial(
                self.executor.retry, conversation_id, task.baseline.version, trusted=True
            )
        raise ValueError("Submit new requirements for a new confirmed plan")

    def decide(
        self,
        conversation_id: str,
        action: Action,
        *,
        version: int | None = None,
        trusted: bool = False,
    ) -> Conversation:
        operation = self.prepare_decision(conversation_id, action, version=version, trusted=trusted)
        return operation() if operation is not None else self.store.get(conversation_id)

    def capability_catalog(self) -> list[dict[str, object]]:
        return self.executor.tools.catalog()

    def workflow_catalog(self) -> list[dict[str, object]]:
        return self.legacy.workflows.catalog() if self.legacy is not None else []
