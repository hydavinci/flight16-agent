import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..jobs import BusyError, JobManager
from .artifacts import verify_artifact
from .contracts import Conversation, PendingDecision, VerificationTask
from .runtime import VerificationRuntime

logger = logging.getLogger(__name__)


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=4000)
    project_root: str | None = Field(default=None, max_length=4096)
    output_root: str | None = Field(default=None, max_length=4096)
    scenario: Literal["d1", "d2"] | None = None

    @field_validator("message")
    @classmethod
    def nonempty_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A nonempty message is required")
        return value.strip()


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
    def __init__(self, runtime: VerificationRuntime, jobs: JobManager):
        self.runtime = runtime
        self.jobs = jobs

    def _fail(self, conversation_id: str, error: Exception) -> None:
        logger.exception("Conversation %s failed", conversation_id)
        record = self.runtime.store.get(conversation_id)
        message = str(error)[:4000] or "Conversation operation failed"
        if record.restore_turn_origin():
            self.runtime._message(record, "assistant", "Follow-up request failed: " + message)
            self.runtime.store.save(record)
            return
        record.pending_decision = PendingDecision(
            kind=(
                record.pending_decision.kind
                if record.pending_decision
                else "resume"
                if record.status != "planning" and record.task and record.task.baseline.confirmed
                else "clarify"
            ),
            message=message,
        )
        record.status = "failed"
        record.error = record.pending_decision.message
        self.runtime.store.save(record)

    def _run(self, conversation_id: str, operation: Callable[[], Conversation]) -> None:
        try:
            operation()
        except Exception as exc:
            # Background failures must be visible in the durable record and the server log.
            self._fail(conversation_id, exc)
        finally:
            self.jobs.slot.release()

    def _dispatch(self, record: Conversation, operation: Callable[[], Conversation]) -> None:
        self.jobs.pool.submit(self._run, record.id, operation)

    def _schedule(self, record: Conversation, operation: Callable[[], Conversation]) -> None:
        try:
            self._dispatch(record, operation)
        except RuntimeError as exc:
            self._fail(record.id, exc)
            raise HTTPException(
                status_code=503, detail="Unable to schedule conversation: " + str(exc)
            ) from exc

    def submit(self, conversation_id: str | None, request: MessageRequest) -> Conversation:
        if not self.jobs.slot.acquire(blocking=False):
            raise BusyError("Another task is running; wait for it to finish")
        dispatched = False
        try:
            record = self.runtime.queue_message(
                conversation_id,
                request.message,
                request.project_root,
                output_root=request.output_root,
                scenario=request.scenario,
            )
            if record.status == "planning":
                self._schedule(record, lambda: self.runtime.continue_submission(record.id))
                dispatched = True
            return record
        finally:
            if not dispatched:
                self.jobs.slot.release()

    def decide(
        self,
        conversation_id: str,
        action: Literal["confirm", "resume", "retry", "replan"],
        request: ConfirmationRequest | None = None,
    ) -> Conversation:
        if not self.jobs.slot.acquire(blocking=False):
            raise BusyError("Another task is running; wait for it to finish")
        dispatched = False
        try:
            record = self.runtime.store.get(conversation_id)
            task = record.task
            if task is None:
                raise ValueError("No verification task has been planned")
            if request is not None and request.version != task.baseline.version:
                raise ValueError("Confirmation baseline version is stale")
            if action in ("confirm", "resume") and record.status == "completed":
                return record
            if action == "confirm":
                if request is None or task.baseline.confirmed:
                    raise ValueError("This baseline is already confirmed; use resume")
                operation = partial(
                    self.runtime.confirm,
                    conversation_id,
                    request.version,
                    trusted=request.trusted,
                )
            elif action == "resume":
                if not task.baseline.confirmed:
                    raise ValueError("Confirm the current baseline before continuing")
                operation = partial(self.runtime.resume, conversation_id)
            elif action == "replan":
                if (
                    request is None
                    or not isinstance(task, VerificationTask)
                    or record.pending_decision is None
                    or record.pending_decision.kind != "confirm_replan"
                    or task.failure_proposal is None
                    or task.failure_proposal.action != "plan"
                ):
                    raise ValueError("No recovery plan is awaiting explicit adoption")
                if task.pending_request and not self.runtime._known_failed_run(task):
                    raise ValueError(
                        "Reconcile the unknown Worker run before adopting another plan"
                    )
                operation = partial(
                    self.runtime.replan,
                    conversation_id,
                    request.version,
                    trusted=request.trusted,
                )
            else:
                if (
                    request is None
                    or not isinstance(task, VerificationTask)
                    or not task.baseline.confirmed
                    or task.pending_request is None
                    or task.worker_run is None
                    or task.worker_run.status not in ("failed", "interrupted")
                ):
                    raise ValueError("Only a known failed or interrupted worker run may be retried")
                operation = partial(
                    self.runtime.retry,
                    conversation_id,
                    request.version,
                    trusted=request.trusted,
                )
            record.status = "running"
            record.error = None
            self.runtime.store.save(record)
            self._schedule(record, operation)
            dispatched = True
            return record
        finally:
            if not dispatched:
                self.jobs.slot.release()


def verification_router(
    runtime: VerificationRuntime, jobs: JobManager, authorize: Callable[[Request], None]
) -> APIRouter:
    router = APIRouter(prefix="/api/conversations", dependencies=[Depends(authorize)])
    manager = ConversationManager(runtime, jobs)

    @contextmanager
    def request_errors() -> Iterator[None]:
        try:
            yield
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown conversation or task") from exc
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
                    "scenario": record.scenario,
                    "project_root": record.project_root,
                    "output_root": record.output_root,
                    "updated_at": record.updated_at,
                    "title": (
                        record.task.proposal.title
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

    @router.get("/{conversation_id}")
    def status(conversation_id: str) -> Conversation:
        with request_errors():
            return runtime.store.get(conversation_id)
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

    @router.post("/{conversation_id}/replan", status_code=202)
    def replan(conversation_id: str, request: ConfirmationRequest) -> Conversation:
        with request_errors():
            return manager.decide(conversation_id, "replan", request)
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
            path = verify_artifact(directory, item)
            return FileResponse(path, media_type=item.media_type, filename=Path(name).name)
        raise AssertionError("Unreachable")

    return router
