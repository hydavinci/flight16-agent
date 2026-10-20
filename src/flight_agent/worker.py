import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .jobs import BusyError, JobManager, JobStore
from .service import (
    BodyLimitMiddleware,
    artifact_response,
    bearer_guard,
    install_validation_handler,
    safe_log,
)
from .upload_engine import execute_upload
from .upload_models import JobRecord, UploadRequest
from .verification.worker import project_routes


def create_worker_app(root: Path, token: str) -> FastAPI:
    uploads_enabled = os.environ.get("MATLAB_ALLOW_TRUSTED_UPLOADS") == "1"
    projects_enabled = os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") == "1"
    if not uploads_enabled and not projects_enabled:
        raise ValueError(
            "Set MATLAB_ALLOW_TRUSTED_PROJECTS=1 or MATLAB_ALLOW_TRUSTED_UPLOADS=1 "
            "for explicitly trusted code only"
        )
    authorize = bearer_guard(token)
    store = JobStore(root / "runs" / "worker")
    manager = JobManager(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        manager.close()

    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware)
    install_validation_handler(app)
    app.include_router(project_routes(root, manager, projects_enabled))

    @app.get("/health")
    def health() -> dict[str, object]:
        from .matlab import engine_available

        return {
            "service": "matlab-worker",
            "engine_package": engine_available(),
            "trusted_code_only": True,
            "project_execution": projects_enabled,
            "upload_execution": uploads_enabled,
        }

    def execute(job_id: str) -> None:
        try:
            output = execute_upload(store.directory(job_id), store.request(job_id))
            store.update(job_id, status="completed", **output.model_dump())
        finally:
            store.update(job_id, log=safe_log(store.directory(job_id)))

    @app.post("/jobs", status_code=202)
    def submit(request: UploadRequest) -> JobRecord:
        if not uploads_enabled:
            raise HTTPException(status_code=403, detail="Trusted uploads are disabled")
        try:
            return manager.submit(request, execute)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/jobs/{job_id}")
    def status(job_id: str) -> JobRecord:
        try:
            return store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown job") from exc

    @app.get("/jobs/{job_id}/artifacts/{name}")
    def artifact(job_id: str, name: str) -> FileResponse:
        return artifact_response(store, job_id, name)

    return app
