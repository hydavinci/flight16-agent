from collections.abc import Callable
from functools import partial
from typing import Literal

from ..runtime.resources import ResourceReference
from .agent_planner import AgentPlanner, bind_plan, message_sources
from .agent_runtime import AgentExecutor, has_unresolved_call
from .contracts import (
    Conversation,
    ConversationMessage,
    ConversationState,
    PendingDecision,
)
from .conversation_repository import ConversationRepository
from .intake import parse_directories, validate_output_root
from .resources import ResourceRegistry, resolve_location
from .scheduling_recovery import conservative_failure_transition
from .store import now

Action = Literal["confirm", "resume", "retry"]


class ConversationActive(ValueError):
    pass


class PlanningSuperseded(ConversationActive):
    pass


class PlanningFailure(RuntimeError):
    def __init__(self, cause: Exception, *, finalized: bool):
        super().__init__(str(cause))
        self.finalized = finalized


class EngineeringRuntime:
    def __init__(
        self,
        store: ConversationRepository,
        planner: AgentPlanner,
        executor: AgentExecutor,
        resources: ResourceRegistry,
    ):
        if executor.store is not store or executor.resources is not resources:
            raise ValueError("Coordinator and executor must share store and resources")
        self.store = store
        self.planner = planner
        self.executor = executor
        self.resources = resources
        self.locks = executor.locks

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
        record.queued_action = None
        record.queued_action_committed = False
        record.queue_origin = None
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
    ) -> Conversation:
        if not message.strip() or len(message) > 4000:
            raise ValueError("A message between 1 and 4000 characters is required")
        selected = resolve_location(location, self.resources.locations())
        identifier = conversation_id if conversation_id is not None else self.store.create().id
        with self.locks.hold(identifier):
            record = self.store.get(identifier)
            if record.status in ("queued", "planning", "running"):
                raise ConversationActive("This conversation already has an admitted turn")
            self._message(record, "user", message.strip())
            if record.task is not None and has_unresolved_call(record.task):
                self._message(
                    record,
                    "assistant",
                    "已有状态未知的调用；请先续接核对原运行，不得切换或重复派发。",
                )
                self.store.save(record)
                return record
            record.turn_origin = (
                ConversationState(
                    status=record.status,
                    pending_decision=record.pending_decision,
                    error=record.error,
                )
                if record.task is not None
                else None
            )
            record.queue_origin = ConversationState(
                status=record.status,
                pending_decision=record.pending_decision,
                error=record.error,
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
            record.status = "queued"
            record.queued_action = "message"
            record.queued_action_committed = False
            record.pending_decision = None
            record.error = None
            self.store.save(record)
            return record

    def continue_submission(
        self,
        conversation_id: str,
        *,
        _managed: bool = False,
    ) -> Conversation:
        with self.locks.hold(conversation_id):
            record = self.store.get(conversation_id)
            if (
                record.status != "queued"
                or record.queued_action != "message"
                or not record.messages
                or record.messages[-1].role != "user"
            ):
                raise ValueError("No queued user message is available for planning")
            record.status = "planning"
            self.store.save(record)
            reservation_stamp = record.updated_at
            message = record.messages[-1].model_copy(deep=True)
            task_id = record.task.id if record.task is not None else None
            if record.queue_origin is None:
                raise ValueError("Queued planning action has no recorded origin")
            queue_origin = record.queue_origin.model_copy(deep=True)
            planning_record = record.model_copy(deep=True)
        try:
            resources: dict[str, ResourceReference] = {}
            if planning_record.project_root:
                if planning_record.resource_location is None:
                    with self.locks.hold(conversation_id):
                        current = self.store.get(conversation_id)
                        return self._wait(current, "请先选择资源位置。")
                resource = self.resources.inspect(
                    planning_record.resource_location,
                    planning_record.project_root,
                )
                resources[resource.id] = resource
            catalog = self.executor.tools.catalog(tuple(resources.values()))
            sources = message_sources(planning_record)
            decision = self.planner.propose(
                message.content,
                planning_record,
                catalog=catalog,
                resources=resources,
                sources=sources,
            )
            plan = (
                bind_plan(decision.plan, resources, sources, catalog)
                if decision.plan is not None
                else None
            )
        except Exception as exc:
            finalized = False
            with self.locks.hold(conversation_id):
                current = self.store.get(conversation_id)
                current_task_id = (
                    current.task.id if current.task is not None else None
                )
                if (
                    current.status == "planning"
                    and current.queued_action == "message"
                    and current.queue_origin == queue_origin
                    and current.updated_at == reservation_stamp
                    and current_task_id == task_id
                    and current.messages
                    and current.messages[-1] == message
                ):
                    error = str(exc)[:4000] or "Conversation planning failed"
                    current.status, current.pending_decision = (
                        conservative_failure_transition(current, error)
                    )
                    current.error = error
                    current.turn_origin = None
                    current.queued_action = None
                    current.queued_action_committed = False
                    current.queue_origin = None
                    self.store.save(current)
                    finalized = True
            if _managed:
                raise PlanningFailure(exc, finalized=finalized) from exc
            raise
        with self.locks.hold(conversation_id):
            record = self.store.get(conversation_id)
            current_task_id = record.task.id if record.task is not None else None
            if (
                record.status != "planning"
                or record.queued_action != "message"
                or record.queue_origin != queue_origin
                or record.updated_at != reservation_stamp
                or current_task_id != task_id
                or not record.messages
                or record.messages[-1] != message
            ):
                conflict = (
                    "Queued planning state changed before its result was applied"
                )
                if _managed:
                    raise PlanningSuperseded(conflict)
                raise ConversationActive(conflict)
            if decision.plan is not None:
                assert plan is not None
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
            record.queued_action = None
            record.queued_action_committed = False
            record.queue_origin = None
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
        with self.locks.hold(conversation_id):
            record = self.store.get(conversation_id)
            task = record.task
            if task is None:
                raise ValueError("No task has been planned")
            if action != "resume" and (
                type(version) is not int
                or version != task.baseline.version
                or trusted is not True
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
                    self.executor.confirm,
                    conversation_id,
                    task.baseline.version,
                    trusted=True,
                )
            if not task.baseline.confirmed:
                raise ValueError("Confirm the current baseline before continuing")
            if record.pending_decision is not None and record.pending_decision.kind not in (
                "resume",
                "retry",
            ):
                raise ValueError(
                    "Address the current follow-up before continuing the previous task"
                )
            if action == "resume":
                return partial(self.executor.resume, conversation_id)
            if action == "retry":
                if has_unresolved_call(task):
                    raise ValueError("Reconcile unknown calls before retrying")
                if not any(node.status == "failed" for node in task.nodes):
                    raise ValueError("Only a known failed invocation may be retried")
                return partial(
                    self.executor.retry,
                    conversation_id,
                    task.baseline.version,
                    trusted=True,
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
        with self.locks.hold(conversation_id):
            record = self.store.get(conversation_id)
            if record.status in ("queued", "planning", "running"):
                if action == "confirm":
                    return self.executor.confirm(
                        conversation_id,
                        version if version is not None else 0,
                        trusted=trusted,
                    )
                if action == "resume":
                    return self.executor.resume(conversation_id)
                if action == "retry":
                    return self.executor.retry(
                        conversation_id,
                        version if version is not None else 0,
                        trusted=trusted,
                    )
            queued, task_id = self._queue_decision(
                conversation_id,
                action,
                version=version,
                trusted=trusted,
            )
            if task_id is None:
                return queued
        return self.executor._run(conversation_id, action, task_id)

    def _queue_decision(
        self,
        conversation_id: str,
        action: Action,
        *,
        version: int | None,
        trusted: bool,
    ) -> tuple[Conversation, str | None]:
        record = self.store.get(conversation_id)
        operation = self.prepare_decision(
            conversation_id,
            action,
            version=version,
            trusted=trusted,
        )
        if operation is None:
            return record, None
        if action == "confirm":
            queued = self.executor.confirm(
                conversation_id,
                version if version is not None else 0,
                trusted=trusted,
                _execute=False,
            )
        elif action == "resume":
            queued = self.executor.resume(conversation_id, _execute=False)
        elif action == "retry":
            queued = self.executor.retry(
                conversation_id,
                version if version is not None else 0,
                trusted=trusted,
                _execute=False,
            )
        else:
            raise ValueError("Submit new requirements for a new confirmed plan")
        if queued.task is None:
            raise ValueError("No task has been planned")
        return queued, queued.task.id

    def continue_decision(
        self,
        conversation_id: str,
        action: Action,
        *,
        version: int | None = None,
        trusted: bool = False,
    ) -> Conversation:
        with self.locks.hold(conversation_id):
            reservation = self.store.get(conversation_id)
            origin = reservation.queue_origin
            if (
                reservation.status != "queued"
                or reservation.queued_action != action
                or reservation.queued_action_committed
                or origin is None
            ):
                raise ValueError("The queued decision is no longer current")
            queued = reservation.model_copy(deep=True)
            queued.status = origin.status
            queued.pending_decision = origin.pending_decision
            queued.error = origin.error
            queued.queue_origin = None
            queued.queued_action = None
            queued.queued_action_committed = False
            if action == "confirm":
                def revalidate() -> None:
                    if self.store.get(conversation_id) != reservation:
                        raise ValueError("The queued decision is no longer current")

                queued, prepared = self.executor._confirm_and_save(
                    queued,
                    version if version is not None else 0,
                    trusted=trusted,
                    revalidate=revalidate,
                )
            else:
                prepared = self.executor._prepare_decision(
                    queued,
                    action,
                    version=version,
                    trusted=trusted,
                )
                if self.store.get(conversation_id) != reservation:
                    raise ValueError("The queued decision is no longer current")
                if not prepared.save:
                    return reservation
                self.store.save(queued)
            task_id = prepared.task_id
            if task_id is None:
                return queued
        return self.executor._run(conversation_id, action, task_id)

    def capability_catalog(self) -> list[dict[str, object]]:
        return self.executor.tools.catalog()
