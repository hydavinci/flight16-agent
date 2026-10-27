import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ..jobs import BusyError, JobManager
from ..runtime.evidence import verify_evidence
from ..runtime.intents import Scenario
from ..runtime.observations import OutputArtifact
from .artifacts import verify_artifact
from .compat import LegacyRuntimeAdapter
from .contracts import AgentTask, Conversation, ConversationMessage, PendingDecision
from .coordinator import EngineeringRuntime
from .runtime import VerificationRuntime
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
    scenario: Scenario | None = None

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
        self, runtime: EngineeringRuntime | VerificationRuntime, jobs: JobManager,
    ):
        self.runtime = (
            LegacyRuntimeAdapter(runtime) if isinstance(runtime, VerificationRuntime) else runtime
        )
        self.jobs = jobs

    def _fail(self, conversation_id: str, error: Exception) -> None:
        logger.exception("Conversation %s failed", conversation_id)
        record = self.runtime.store.get(conversation_id)
        message = str(error)[:4000] or "Conversation operation failed"
        if record.restore_turn_origin():
            record.messages.append(ConversationMessage(
                role="assistant", content="Follow-up request failed: " + message, created_at=now(),
            ))
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
                request.canonical_message(),
                request.project_root,
                output_root=request.output_root,
                location=request.location,
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
            operation = self.runtime.prepare_decision(
                conversation_id, action,
                version=request.version if request is not None else None,
                trusted=request.trusted if request is not None else False,
            )
            if operation is None:
                return record
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
    runtime: EngineeringRuntime | VerificationRuntime, jobs: JobManager,
    authorize: Callable[[Request], None],
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
                        record.task.baseline.title
                        if isinstance(record.task, AgentTask)
                        else record.task.proposal.title
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

    @router.get("/workflows")
    def workflows() -> list[dict[str, object]]:
        return manager.runtime.workflow_catalog()

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
            if isinstance(item, OutputArtifact):
                path = verify_evidence(directory, item)
                return FileResponse(path, filename=Path(name).name)
            path = verify_artifact(directory, item)
            return FileResponse(path, media_type=item.media_type, filename=Path(name).name)
        raise AssertionError("Unreachable")

    return router
