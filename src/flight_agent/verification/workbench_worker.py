import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..background import BackgroundRunner, BusyError
from ..service import safe_log
from ..workbench_contracts import (
    WorkbenchArtifact,
    WorkbenchRunRecord,
    WorkbenchRunRequest,
    workbench_request_digest,
)
from .artifacts import artifact_path
from .projects import file_digest
from .workbench_matlab import execute_workbench, validate_workbench_png
from .workbench_store import WorkbenchRunStore

logger = logging.getLogger(__name__)


def _error_text(exc: Exception) -> str:
    return (str(exc) or type(exc).__name__)[:6000]


def _verified_artifact(directory: Path, artifact: WorkbenchArtifact) -> Path:
    path = artifact_path(directory, artifact.path)
    validate_workbench_png(path)
    if path.stat().st_size != artifact.size or file_digest(path) != artifact.sha256:
        raise ValueError(f"Artifact integrity mismatch: {artifact.path}")
    return path


def workbench_routes(root: Path, runner: BackgroundRunner) -> APIRouter:
    store = WorkbenchRunStore(root / "runs" / "workbench-worker")
    router = APIRouter(prefix="/workbench")

    def execute(run_id: str) -> None:
        directory = store.directory(run_id) / "execution"
        try:
            store.update(run_id, status="running", stage="executing")
            receipt, artifacts, log = execute_workbench(directory, store.request(run_id))
            store.update(
                run_id,
                status="completed",
                stage="completed",
                result=receipt.result,
                matlab_version=receipt.matlab_version,
                log=log,
                artifacts=artifacts,
            )
        except Exception as exc:
            logger.exception("Workbench run %s failed", run_id)
            store.update(
                run_id,
                status="failed",
                stage="failed",
                error=_error_text(exc),
                log=safe_log(directory),
            )
        finally:
            runner.slot.release()

    @router.get("/runs")
    def recent() -> list[WorkbenchRunRecord]:
        return store.recent()

    @router.post("/runs", status_code=202)
    def submit(request: WorkbenchRunRequest) -> WorkbenchRunRecord:
        with store.lock:
            try:
                existing = store.get(request.id)
            except KeyError:
                existing = None
            if existing is not None:
                if existing.request_sha256 != workbench_request_digest(request):
                    raise HTTPException(status_code=409, detail="Run ID has different input")
                return existing
            if not runner.slot.acquire(blocking=False):
                raise HTTPException(status_code=409, detail=str(BusyError("Worker is busy")))
            dispatched = False
            try:
                record = store.create(request)
                try:
                    runner.pool.submit(execute, record.id)
                except RuntimeError as exc:
                    logger.exception("Unable to schedule workbench run %s", record.id)
                    error = _error_text(exc)
                    store.update(
                        record.id,
                        status="failed",
                        stage="failed",
                        error=error,
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="Unable to schedule workbench run: " + error,
                    ) from exc
                dispatched = True
                return record
            finally:
                if not dispatched:
                    runner.slot.release()

    @router.get("/runs/{run_id}")
    def status(run_id: str) -> WorkbenchRunRecord:
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown run") from exc

    @router.get("/runs/{run_id}/artifacts/{name:path}")
    def artifact(run_id: str, name: str) -> FileResponse:
        record = status(run_id)
        entry = next((item for item in record.artifacts if item.path == name), None)
        if record.status != "completed" or entry is None:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        try:
            path = _verified_artifact(store.directory(run_id) / "execution", entry)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type=entry.media_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return router
