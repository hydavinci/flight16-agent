import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ..directories import DirectoryListing, browse_directories
from ..jobs import BusyError, JobManager
from ..service import safe_log
from .artifacts import verify_artifact
from .contracts import AnalysisRunRequest, InspectProjectRequest, ProjectDescriptor, RunRecord
from .matlab import execute_project
from .projects import ProjectInspector
from .store import WorkerRunStore, request_digest

logger = logging.getLogger(__name__)


def project_routes(root: Path, manager: JobManager, enabled: bool) -> APIRouter:
    store = WorkerRunStore(root / "runs" / "verification-worker")

    def authorize_projects() -> None:
        if not enabled:
            raise HTTPException(
                status_code=403, detail="Set MATLAB_ALLOW_TRUSTED_PROJECTS=1 on the worker"
            )

    router = APIRouter(prefix="/verification", dependencies=[Depends(authorize_projects)])

    @router.get("/directories")
    def directories(path: str = Query(default="", max_length=4096)) -> DirectoryListing:
        try:
            return browse_directories(path)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/projects/inspect")
    def inspect(request: InspectProjectRequest) -> ProjectDescriptor:
        try:
            return ProjectInspector().inspect(request.root)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def execute(run_id: str) -> None:
        def progress(stage: str) -> None:
            store.update(run_id, stage=stage)

        try:
            store.update(run_id, status="running", stage="preparing")
            result = execute_project(
                store.directory(run_id),
                store.request(run_id),
                on_progress=progress,
            )
            store.update(run_id, result=result.model_dump(), status="completed", stage="completed")
        except Exception as exc:
            # A background exception must be observable by the submitting client.
            logger.exception("Verification run %s failed", run_id)
            store.update(run_id, status="failed", error=str(exc)[:6000])
        finally:
            try:
                store.update(run_id, log=safe_log(store.directory(run_id)))
            finally:
                manager.slot.release()

    @router.post("/runs", status_code=202)
    def submit(request: AnalysisRunRequest) -> RunRecord:
        with store.lock:
            try:
                existing = store.get(request.id)
            except KeyError:
                existing = None
            if existing is not None:
                if existing.request_sha256 != request_digest(request):
                    raise HTTPException(status_code=409, detail="Run ID has different input")
                return existing
            if not manager.slot.acquire(blocking=False):
                raise HTTPException(status_code=409, detail=str(BusyError("Worker is busy")))
            dispatched = False
            try:
                record = store.create(request)
                try:
                    manager.pool.submit(execute, record.id)
                except RuntimeError as exc:
                    logger.exception("Unable to schedule verification run %s", record.id)
                    store.update(record.id, status="failed", stage="failed", error=str(exc)[:6000])
                    raise HTTPException(
                        status_code=503, detail="Unable to schedule verification run: " + str(exc)
                    ) from exc
                dispatched = True
                return record
            finally:
                if not dispatched:
                    manager.slot.release()

    @router.get("/runs/{run_id}")
    def status(run_id: str) -> RunRecord:
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown run") from exc

    @router.get("/runs/{run_id}/artifacts/{name:path}")
    def artifact(run_id: str, name: str) -> FileResponse:
        record = status(run_id)
        result = record.result
        entry = (
            next((item for item in result.artifacts if item.path == name), None) if result else None
        )
        if record.status != "completed" or entry is None:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        try:
            path = verify_artifact(store.directory(run_id), entry)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            path, media_type=entry.media_type, headers={"X-Content-Type-Options": "nosniff"}
        )

    return router
