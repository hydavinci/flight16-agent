import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from .agent import execute_agent
from .config import ModelSettings, create_model
from .jobs import BusyError, JobManager, JobStore
from .models import AnalysisResult, Parameters, validate_result_context
from .remote import RemoteWorker
from .service import BodyLimitMiddleware, artifact_response, install_validation_handler, safe_log
from .session import AnalysisSession
from .upload_models import JobRecord, UploadRequest
from .verification.routes import verification_router
from .verification.runtime import VerificationRuntime


class AnalysisRequest(BaseModel):
    request: str = Field(min_length=1, max_length=2000)


class CachedRunner:
    def __init__(self, result: AnalysisResult, log: str):
        self.result = result
        self.log = log

    def run(self, parameters: Parameters) -> AnalysisResult:
        validate_result_context(self.result, parameters)
        return self.result


def create_web_app(
    root: Path, *, verification_runtime: VerificationRuntime | None = None
) -> FastAPI:
    root = root.resolve()
    store = JobStore(root / "runs" / "web")
    manager = JobManager(store)
    token = secrets.token_urlsafe(32)
    static = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        manager.close()

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if (
            origin is not None
            and origin != expected
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise HTTPException(status_code=403, detail="Cross-origin requests are not allowed")

    def authorize(request: Request) -> None:
        supplied = request.headers.get("x-flight-token", "")
        if not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=403, detail="Reload this page to obtain its API token")

    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(same_origin)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    install_validation_handler(app)
    auth = [Depends(authorize)]
    verification_runtime = verification_runtime or VerificationRuntime(
        root / "runs" / "verification"
    )
    app.include_router(verification_router(verification_runtime, manager, authorize))

    @app.middleware("http")
    async def response_headers(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "agent.html", media_type="text/html")

    @app.get("/workbench")
    def workbench() -> FileResponse:
        return FileResponse(static / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    def asset(name: str) -> FileResponse:
        if name not in ("app.js", "style.css", "agent.js", "agent.css"):
            raise HTTPException(status_code=404, detail="Unknown asset")
        return FileResponse(static / name)

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, object]:
        worker_error = model_error = None
        try:
            RemoteWorker.from_environment()
        except ValueError as exc:
            worker_error = str(exc)
        try:
            ModelSettings.from_environment()
        except ValueError as exc:
            model_error = str(exc)
        return {
            "token": token,
            "worker_configured": worker_error is None,
            "worker_error": worker_error,
            "model_configured": model_error is None,
            "model_error": model_error,
            "trusted_code_only": True,
        }

    @app.get("/api/examples/{name}", dependencies=auth)
    def example(name: str) -> FileResponse:
        if name not in ("web_script.m", "web_function.m", "web_metrics.m", "parameters.json"):
            raise HTTPException(status_code=404, detail="Unknown example")
        path = root / "examples" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Example missing from project directory")
        return FileResponse(path, media_type="text/plain", filename=name)

    @app.get("/api/jobs", dependencies=auth)
    def recent() -> list[dict[str, object]]:
        return [
            record.model_dump(include={"id", "filename", "status", "created_at", "agent_status"})
            for record in store.recent()
        ]

    @app.post("/api/jobs", status_code=202, dependencies=auth)
    def submit(request: UploadRequest) -> JobRecord:
        try:
            remote = RemoteWorker.from_environment()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        def execute(job_id: str) -> None:
            def submitted(remote_id: str) -> None:
                store.update(job_id, remote_id=remote_id)

            try:
                output = remote.run(store.directory(job_id), request, submitted)
                store.update(job_id, status="completed", **output.model_dump())
            finally:
                store.update(job_id, log=safe_log(store.directory(job_id)))

        try:
            return manager.submit(request, execute)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}", dependencies=auth)
    def status(job_id: str) -> JobRecord:
        try:
            return store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown job") from exc

    @app.get("/api/jobs/{job_id}/result", dependencies=auth)
    def result_json(job_id: str) -> Response:
        record = status(job_id)
        if record.status != "completed":
            raise HTTPException(status_code=409, detail="Wait for successful MATLAB execution")
        return Response(
            content=json.dumps(record.result, ensure_ascii=False, indent=2, allow_nan=False),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="result.json"'},
        )

    @app.get("/api/jobs/{job_id}/artifacts/{name}", dependencies=auth)
    def artifact(job_id: str, name: str) -> FileResponse:
        return artifact_response(store, job_id, name)

    def analyze_result(job_id: str, instruction: str) -> None:
        record = store.get(job_id)
        upload = store.request(job_id)
        try:
            parameters = Parameters.model_validate(upload.parameters)
            result = AnalysisResult.model_validate(record.result)
            validate_result_context(result, parameters)
        except (ValidationError, ValueError):
            reason = (
                "无法按当前规则判定：参数或 result 不符合现有飞行器指标格式，"
                "或机型/工况不匹配。原始 MATLAB 结果仍可查看；需接入适用规则和数据适配。"
            )
            store.update(
                job_id,
                agent_status="undetermined",
                agent_error=reason,
                assessment={"status": "undetermined", "reason": reason},
            )
            return
        session = AnalysisSession(
            root,
            parameters,
            CachedRunner(result, record.log),
            source=upload.source.encode("utf-8"),
            entry=upload.filename[:-2],
        )
        store.update(job_id, agent_run_id=str(session.metadata["run_id"]))
        try:
            model = create_model()
            session.metadata["model_name"] = getattr(model, "model_name", type(model).__name__)
            path = execute_agent(model, session, instruction)
            store.update(
                job_id,
                agent_status="completed",
                report=path.read_text("utf-8"),
                assessment=session.assessment.model_dump() if session.assessment else None,
            )
        finally:
            if session.metadata["status"] != "completed" and session.error is None:
                session.fail("Agent analysis did not complete; original Web execution is retained")

    @app.post("/api/jobs/{job_id}/analyze", status_code=202, dependencies=auth)
    def analyze(job_id: str, request: AnalysisRequest) -> JobRecord:
        record = status(job_id)
        if record.status != "completed":
            raise HTTPException(status_code=409, detail="Wait for successful MATLAB execution")
        if record.agent_status in ("running", "completed"):
            raise HTTPException(
                status_code=409, detail="Agent analysis already started or completed"
            )
        try:
            manager.analyze(job_id, lambda identifier: analyze_result(identifier, request.request))
            return store.get(job_id)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
