import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition
from typing import Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ..copilot_diagnostics import CopilotError
from ..runtime.evidence import verify_evidence
from ..runtime.scheduling import (
    CapacityExceeded,
    ExecutionTurnScheduler,
    PlanningScheduler,
    SchedulerClosed,
    Submission,
)
from .contracts import (
    Conversation,
    ConversationMessage,
    ConversationState,
)
from .coordinator import (
    ConversationActive,
    EngineeringRuntime,
    PlanningFailure,
    PlanningSuperseded,
)
from .scheduling_recovery import (
    conservative_failure_transition,
    conservative_pending_decision,
    has_unresolved_scheduling,
)
from .store import now
from .views import ConversationView, to_conversation_view

logger = logging.getLogger(__name__)


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str | None = Field(default=None, min_length=1, max_length=4000)
    analysis_request: str | None = Field(default=None, min_length=1, max_length=4000)
    evaluation_criteria: str | None = Field(default=None, min_length=1, max_length=4000)
    project_root: str | None = Field(default=None, max_length=4096)
    output_root: str | None = Field(default=None, max_length=4096)
    location: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("message", "analysis_request", "evaluation_criteria")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Nonempty request text is required")
        return value

    @model_validator(mode="after")
    def message_or_structured_input(self) -> Self:
        if self.message is not None:
            if self.analysis_request is not None or self.evaluation_criteria is not None:
                raise ValueError("message cannot be combined with structured input fields")
        elif self.analysis_request is None:
            raise ValueError("Provide message or analysis_request")
        if len(self.canonical_message()) > 4000:
            raise ValueError(
                "Combined message including source labels must not exceed 4000 characters"
            )
        return self

    def canonical_message(self) -> str:
        if self.message is not None:
            return self.message.strip()
        text = f"[工程分析要求]\n{self.analysis_request}\n[工程分析要求结束]"
        if self.evaluation_criteria is not None:
            text += f"\n\n[评价指标]\n{self.evaluation_criteria}\n[评价指标结束]"
        return text


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(gt=0)
    trusted: StrictBool

    @field_validator("trusted")
    @classmethod
    def explicit_consent(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Explicit trusted-project consent is required")
        return value


class ConversationManager:
    def __init__(
        self,
        runtime: EngineeringRuntime,
        planning: PlanningScheduler,
        execution: ExecutionTurnScheduler,
    ) -> None:
        self.runtime = runtime
        self.planning = planning
        self.execution = execution
        self._close_condition = Condition()
        self._admission_stopped = False
        self._draining = False
        self._closed = False

    def close(self) -> None:
        with self._close_condition:
            if self._closed:
                return
            if not self._admission_stopped:
                self.planning.stop_accepting()
                self.execution.stop_accepting()
                self._admission_stopped = True
        if self.planning.is_worker_thread() or self.execution.is_worker_thread():
            return
        with self._close_condition:
            while self._draining and not self._closed:
                self._close_condition.wait()
            if self._closed:
                return
            self._draining = True
        try:
            self.planning.close()
            self.execution.close()
        except BaseException:
            with self._close_condition:
                self._draining = False
                self._close_condition.notify_all()
            raise
        with self._close_condition:
            self._closed = True
            self._draining = False
            self._close_condition.notify_all()

    def _fail(
        self,
        conversation_id: str,
        error: Exception,
        *,
        restore_unstarted: bool = True,
    ) -> None:
        logger.exception("Conversation %s failed", conversation_id)
        with self.runtime.locks.hold(conversation_id):
            record = self.runtime.store.get(conversation_id)
            message = str(error)[:4000] or "Conversation operation failed"
            action = record.queued_action
            origin = record.queue_origin
            committed = (
                record.queued_action_committed
                and action in ("confirm", "resume", "retry")
            )
            record.queued_action = None
            record.queued_action_committed = False
            record.queue_origin = None
            if committed:
                unresolved = has_unresolved_scheduling(record)
                failed = record.task is not None and any(
                    node.status == "failed" for node in record.task.nodes
                )
                confirmed = (
                    record.task is not None
                    and record.task.baseline.confirmed
                )
                record.status = (
                    "interrupted"
                    if unresolved or confirmed and not failed
                    else "failed"
                )
                record.error = message
                record.pending_decision = conservative_pending_decision(
                    record,
                    message,
                    unresolved=unresolved,
                )
            else:
                may_restore = (
                    restore_unstarted
                    or action not in ("confirm", "resume", "retry")
                )
                restored = record.restore_turn_origin() if may_restore else False
                if (
                    may_restore
                    and not restored
                    and origin is not None
                    and (action != "message" or len(record.messages) > 1)
                ):
                    record.status = origin.status
                    record.pending_decision = origin.pending_decision
                    record.error = origin.error
                    restored = True
                if restored:
                    record.messages.append(
                        ConversationMessage(
                            role="assistant",
                            content="Request failed: " + message,
                            created_at=now(),
                        )
                    )
                else:
                    record.status, record.pending_decision = (
                        conservative_failure_transition(record, message)
                    )
                    record.error = message
            record.turn_origin = None
            self.runtime.store.save(record)

    def _run(self, conversation_id: str, operation: Callable[[], Conversation]) -> None:
        with self.runtime.locks.hold(conversation_id):
            try:
                operation()
            except PlanningSuperseded:
                logger.exception(
                    "Conversation %s planning result was superseded",
                    conversation_id,
                )
            except PlanningFailure as exc:
                state = "finalized" if exc.finalized else "stale"
                if isinstance(exc.cause, CopilotError):
                    logger.error(
                        "Copilot planning failed for conversation %s "
                        "reservation_state=%s category=%s exit_code=%s",
                        conversation_id,
                        state,
                        exc.cause.diagnostics.category,
                        exc.cause.diagnostics.exit_code,
                        exc_info=False,
                    )
                else:
                    logger.exception(
                        "Conversation %s planning failed with %s reservation state",
                        conversation_id,
                        state,
                    )
            except Exception as exc:
                self._fail(conversation_id, exc, restore_unstarted=False)

    def _schedule(
        self,
        record: Conversation,
        reservation: Submission,
        operation: Callable[[], Conversation],
    ) -> Conversation:
        response = record.model_copy(deep=True)
        try:
            reservation.submit(lambda: self._run(record.id, operation))
        except RuntimeError as exc:
            self._fail(record.id, exc)
            raise SchedulerClosed(
                "Unable to publish reserved conversation work"
            ) from exc
        return response

    def submit(self, conversation_id: str | None, request: MessageRequest) -> Conversation:
        if conversation_id is not None:
            current = self.runtime.store.get(conversation_id)
            if current.status in ("queued", "planning", "running"):
                raise ConversationActive(
                    "This conversation already has an admitted turn"
                )
        with self.planning.reserve() as reservation:
            if conversation_id is None:
                record = self.runtime.queue_message(
                    None,
                    request.canonical_message(),
                    request.project_root,
                    output_root=request.output_root,
                    location=request.location,
                )
                identifier = record.id
            else:
                identifier = conversation_id
                record = None
            with self.runtime.locks.hold(identifier):
                if record is None:
                    record = self.runtime.queue_message(
                        identifier,
                        request.canonical_message(),
                        request.project_root,
                        output_root=request.output_root,
                        location=request.location,
                    )
                if record.status != "queued" or record.queued_action != "message":
                    return record.model_copy(deep=True)
                return self._schedule(
                    record,
                    reservation,
                    lambda: self.runtime.continue_submission(
                        identifier,
                        _managed=True,
                    ),
                )

    def _decision(
        self,
        conversation_id: str,
        action: Literal["confirm", "resume", "retry"],
        request: ConfirmationRequest | None,
    ) -> Conversation:
        return self.runtime.continue_decision(
            conversation_id,
            action,
            version=request.version if request is not None else None,
            trusted=request.trusted if request is not None else False,
        )

    def decide(
        self,
        conversation_id: str,
        action: Literal["confirm", "resume", "retry"],
        request: ConfirmationRequest | None = None,
    ) -> Conversation:
        current = self.runtime.store.get(conversation_id)
        if current.status in ("queued", "planning", "running"):
            raise ConversationActive(
                "This conversation already has an admitted turn"
            )
        with self.execution.reserve() as reservation:
            with self.runtime.locks.hold(conversation_id):
                record = self.runtime.store.get(conversation_id)
                if record.status in ("queued", "planning", "running"):
                    raise ConversationActive(
                        "This conversation already has an admitted turn"
                    )
                operation = self.runtime.prepare_decision(
                    conversation_id,
                    action,
                    version=request.version if request is not None else None,
                    trusted=request.trusted if request is not None else False,
                )
                if operation is None:
                    return record.model_copy(deep=True)
                record.queue_origin = ConversationState(
                    status=record.status,
                    pending_decision=record.pending_decision,
                    error=record.error,
                )
                record.queued_action = action
                record.queued_action_committed = False
                record.status = "queued"
                self.runtime.store.save(record)
                return self._schedule(
                    record,
                    reservation,
                    lambda: self._decision(conversation_id, action, request),
                )


def verification_router(
    manager: ConversationManager,
    authorize: Callable[[Request], None],
) -> APIRouter:
    runtime = manager.runtime
    router = APIRouter(prefix="/api/conversations", dependencies=[Depends(authorize)])

    @contextmanager
    def request_errors() -> Iterator[None]:
        try:
            yield
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown conversation or task") from exc
        except CapacityExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail={"code": "scheduler_capacity", "message": str(exc)},
                headers={"Retry-After": "1"},
            ) from exc
        except SchedulerClosed as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "scheduler_closed", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            logger.exception("Conversation storage is unavailable")
            raise HTTPException(
                status_code=503, detail="Conversation storage is unavailable"
            ) from exc

    @router.get("")
    def recent() -> list[dict[str, str | None]]:
        with request_errors():
            return [
                {
                    "id": record.id,
                    "status": record.status,
                    "project_root": record.project_root,
                    "output_root": record.output_root,
                    "updated_at": record.updated_at,
                    "title": (
                        record.task.baseline.title
                        if record.task
                        else record.messages[0].content[:100]
                        if record.messages
                        else "新会话"
                    ),
                }
                for record in runtime.store.recent()
            ]
        raise AssertionError("Unreachable")

    @router.post("", status_code=202)
    def create(request: MessageRequest) -> Conversation:
        with request_errors():
            return manager.submit(None, request)
        raise AssertionError("Unreachable")

    @router.get("/capabilities")
    def capabilities() -> list[dict[str, object]]:
        return manager.runtime.capability_catalog()

    @router.get("/{conversation_id}")
    def status(conversation_id: str) -> Conversation:
        with request_errors():
            return runtime.store.get(conversation_id)
        raise AssertionError("Unreachable")

    @router.get("/{conversation_id}/view")
    def view(conversation_id: str) -> ConversationView:
        with request_errors():
            return to_conversation_view(runtime.store.get(conversation_id))
        raise AssertionError("Unreachable")

    @router.post("/{conversation_id}/messages", status_code=202)
    def message(conversation_id: str, request: MessageRequest) -> Conversation:
        with request_errors():
            return manager.submit(conversation_id, request)
        raise AssertionError("Unreachable")

    @router.post("/{conversation_id}/confirm", status_code=202)
    def confirm(conversation_id: str, request: ConfirmationRequest) -> Conversation:
        with request_errors():
            return manager.decide(conversation_id, "confirm", request)
        raise AssertionError("Unreachable")

    @router.post("/{conversation_id}/resume", status_code=202)
    def resume(conversation_id: str) -> Conversation:
        with request_errors():
            return manager.decide(conversation_id, "resume")
        raise AssertionError("Unreachable")

    @router.post("/{conversation_id}/retry", status_code=202)
    def retry(conversation_id: str, request: ConfirmationRequest) -> Conversation:
        with request_errors():
            return manager.decide(conversation_id, "retry", request)
        raise AssertionError("Unreachable")

    @router.get("/{conversation_id}/tasks/{task_id}/artifacts/{name:path}")
    def artifact(conversation_id: str, task_id: str, name: str) -> FileResponse:
        with request_errors():
            record = runtime.store.get(conversation_id)
            tasks = ([record.task] if record.task else []) + record.archived_tasks
            task = next((task for task in tasks if task.id == task_id), None)
            if task is None:
                raise KeyError("Unknown task")
            item = next((artifact for artifact in task.artifacts if artifact.path == name), None)
            if item is None:
                raise HTTPException(status_code=404, detail="Unknown artifact")
            directory = runtime.store.task_directory(conversation_id, task_id, task.output_root)
            if str(directory) != task.result_directory:
                raise ValueError("Task result directory does not match its persisted identity")
            path = verify_evidence(directory, item)
            return FileResponse(path, filename=Path(name).name)
        raise AssertionError("Unreachable")

    return router
