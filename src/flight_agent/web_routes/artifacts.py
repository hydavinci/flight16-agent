from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..runtime.evidence import verify_evidence
from ..verification.coordinator import EngineeringRuntime
from .errors import request_errors


def artifact_router(
    runtime: EngineeringRuntime,
    authorize: Callable[[Request], None],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/conversations",
        dependencies=[Depends(authorize)],
    )

    @router.get("/{conversation_id}/tasks/{task_id}/artifacts/{name:path}")
    def artifact(conversation_id: str, task_id: str, name: str) -> FileResponse:
        with request_errors():
            record = runtime.store.get(conversation_id)
            tasks = ([record.task] if record.task else []) + record.archived_tasks
            task = next((task for task in tasks if task.id == task_id), None)
            if task is None:
                raise KeyError("Unknown task")
            item = next(
                (artifact for artifact in task.artifacts if artifact.path == name),
                None,
            )
            if item is None:
                raise HTTPException(status_code=404, detail="Unknown artifact")
            directory = runtime.store.task_directory(
                conversation_id,
                task_id,
                task.output_root,
            )
            if str(directory) != task.result_directory:
                raise ValueError(
                    "Task result directory does not match its persisted identity"
                )
            path = verify_evidence(directory, item)
            return FileResponse(path, filename=Path(name).name)
        raise AssertionError("Unreachable")

    return router
