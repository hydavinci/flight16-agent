import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from .config import ModelSettings, SchedulerSettings
from .directories import (
    DirectoryListing,
    NativeDirectoryError,
    browse_directories,
    choose_native_directory,
)
from .remote import RemoteError
from .runtime.capabilities import CapabilityRegistry
from .runtime.scheduling import (
    ExecutionScheduler,
    ExecutionTurnScheduler,
    PlanningScheduler,
)
from .service import BodyLimitMiddleware, install_validation_handler
from .verification.agent_planner import AgentPlanner
from .verification.agent_runtime import AgentContext, AgentExecutor
from .verification.backend import AnalysisBackend, RemoteSimulinkBackend
from .verification.coordinator import EngineeringRuntime
from .verification.diagnostic_planner import DiagnosticReviewer
from .verification.local_matlab import LocalMatlabBackend
from .verification.plugins import register_builtin_plugins
from .verification.resources import ResourceLocation, ResourceRegistry, resolve_location
from .verification.routes import ConversationManager, verification_router
from .verification.sqlite_store import SQLiteConversationStore


class DirectorySelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: Literal["project", "output"]
    location: str | None = Field(default=None, min_length=1, max_length=128)
    path: str = Field(default="", max_length=4096)


def create_web_app(
    root: Path,
    *,
    engineering_runtime: EngineeringRuntime | None = None,
    native_directory_picker: bool = False,
    scheduler_settings: SchedulerSettings | None = None,
) -> FastAPI:
    settings = scheduler_settings if scheduler_settings is not None else (
        engineering_runtime.executor.scheduler.settings
        if engineering_runtime is not None
        else SchedulerSettings.from_environment()
    )
    if (
        engineering_runtime is not None
        and engineering_runtime.executor.scheduler.settings != settings
    ):
        raise ValueError("Injected runtime and Web scheduler settings must match")
    local_backend = LocalMatlabBackend.from_environment()
    resources = (
        engineering_runtime.resources if engineering_runtime is not None else ResourceRegistry()
    )
    try:
        backend: AnalysisBackend | None = RemoteSimulinkBackend.from_environment()
    except ValueError:
        backend = None
    if engineering_runtime is None:
        conversations = SQLiteConversationStore(root / "runs" / "verification")
        tools: CapabilityRegistry[AgentContext] = CapabilityRegistry(agent_mode=True)
        register_builtin_plugins(
            tools, resources, backend=backend,
            diagnostic_reviewer=DiagnosticReviewer(), local_backend=local_backend,
        )
        engineering_runtime = EngineeringRuntime(
            conversations, AgentPlanner(),
            AgentExecutor(
                conversations,
                tools,
                resources,
                scheduler=ExecutionScheduler(settings),
            ),
            resources,
        )
    runtime = engineering_runtime
    manager = ConversationManager(
        runtime,
        PlanningScheduler(settings.planning_workers, settings.planning_queue),
        ExecutionTurnScheduler(settings.execution_turns),
    )
    token = secrets.token_urlsafe(32)
    static = Path(__file__).parent / "static"
    native_picker_lock = Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
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
    app.state.conversation_manager = manager
    app.include_router(verification_router(manager, authorize))

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
        return FileResponse(static / "agent.html", media_type="text/html")

    @app.get("/static/{name}")
    def asset(name: str) -> FileResponse:
        if name not in {"agent.js", "agent.css", "favicon.svg"}:
            raise HTTPException(status_code=404, detail="Unknown asset")
        return FileResponse(static / name)

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, object]:
        local = local_backend.readiness()
        worker_error = model_error = None
        try:
            RemoteSimulinkBackend.from_environment()
        except ValueError as exc:
            worker_error = str(exc)
        if runtime.planner.model is None and type(runtime.planner) is AgentPlanner:
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
            "local_matlab": {
                "state": local.state,
                "reason": (
                    "Import probe only; MATLAB startup and license are not verified."
                    if local.state == "available"
                    else "Local MATLAB Engine unavailable; enable MATLAB_ALLOW_TRUSTED_PROJECTS=1 "
                    "and check the selected Python environment and Engine installation."
                ),
            },
            "trusted_code_only": True,
            "native_directory_picker": native_directory_picker,
        }

    @app.get("/api/resource-locations", dependencies=auth)
    def resource_locations() -> list[ResourceLocation]:
        return resources.locations()

    def project_location(location: str | None) -> ResourceLocation:
        locations = resources.locations()
        try:
            identifier = resolve_location(location, locations)
            if identifier is None:
                if len(locations) != 1:
                    raise ValueError("Select an explicit registered resource location")
                identifier = locations[0].id
            return next(item for item in locations if item.id == identifier)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/directories", dependencies=auth)
    def directories(
        target: Literal["project", "output"],
        path: str = Query(default="", max_length=4096),
        location: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> DirectoryListing:
        if target == "project":
            selected = project_location(location)
            if selected.availability.state != "available":
                raise HTTPException(status_code=503, detail=selected.availability.reason)
            try:
                return resources.browse(selected.id, path)
            except RemoteError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return browse_directories(path)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/directories/select", dependencies=auth)
    def select_directory(request: DirectorySelectionRequest) -> dict[str, str | None]:
        if not native_directory_picker:
            raise HTTPException(
                status_code=409,
                detail="Native directory selection requires --native-directory-picker",
            )
        selected = (
            project_location(request.location)
            if request.target == "project" else None
        )
        if selected is not None and selected.location == "worker":
            if not isinstance(backend, RemoteSimulinkBackend):
                raise HTTPException(status_code=503, detail="Worker is not configured")
            if urlsplit(backend.url).hostname not in ("127.0.0.1", "localhost", "::1"):
                raise HTTPException(
                    status_code=409,
                    detail="Native project selection requires the Worker on the same machine",
                )
        if not native_picker_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A native directory picker is already open")
        try:
            return {
                "path": choose_native_directory(
                    request.path,
                    title=(
                        "选择资源目录" if request.target == "project"
                        else "选择输出根目录"
                    ),
                )
            }
        except NativeDirectoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            native_picker_lock.release()

    return app
