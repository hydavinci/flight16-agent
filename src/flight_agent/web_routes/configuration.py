from collections.abc import Callable
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..application import ApplicationServices
from ..config import ModelSettings
from ..directories import (
    DirectoryListing,
    NativeDirectoryError,
    browse_directories,
)
from ..remote import RemoteError
from ..runtime.capability_metadata import Availability
from ..verification.agent_planner import AgentPlanner
from ..verification.backend import RemoteSimulinkBackend
from ..verification.resources import ResourceLocation, resolve_location


class DirectorySelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: Literal["project", "output"]
    location: str | None = Field(default=None, min_length=1, max_length=128)
    path: str = Field(default="", max_length=4096)


def configuration_router(
    services: ApplicationServices,
    token: str,
    native_directory_picker: bool,
    picker: Callable[..., str | None],
    authorize: Callable[[Request], None],
) -> APIRouter:
    router = APIRouter()
    runtime = services.runtime
    resources = services.resources
    local_backend = services.local_backend
    backend = services.backend
    native_picker_lock = Lock()
    auth = [Depends(authorize)]
    choose_native_directory = picker

    @router.get("/api/bootstrap")
    def bootstrap() -> dict[str, object]:
        local = (
            local_backend.readiness()
            if local_backend is not None
            else Availability(
                state="unavailable",
                reason="The injected runtime has no local MATLAB capability",
            )
        )
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
                    local.reason
                    if local_backend is None
                    else (
                        "Import probe only; MATLAB startup and license are not verified."
                        if local.state == "available"
                        else "Local MATLAB Engine unavailable; "
                        "enable MATLAB_ALLOW_TRUSTED_PROJECTS=1 and check the selected "
                        "Python environment and Engine installation."
                    )
                ),
            },
            "trusted_code_only": True,
            "native_directory_picker": native_directory_picker,
        }

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

    @router.get("/api/directories", dependencies=auth)
    def directories(
        target: Literal["project", "output"],
        path: str = Query(default="", max_length=4096),
        location: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> DirectoryListing:
        if target == "project":
            selected = project_location(location)
            if selected.availability.state != "available":
                raise HTTPException(
                    status_code=503,
                    detail=selected.availability.reason,
                )
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

    @router.post("/api/directories/select", dependencies=auth)
    def select_directory(
        request: DirectorySelectionRequest,
    ) -> dict[str, str | None]:
        if not native_directory_picker:
            raise HTTPException(
                status_code=409,
                detail="Native directory selection requires --native-directory-picker",
            )
        selected = (
            project_location(request.location)
            if request.target == "project"
            else None
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
            raise HTTPException(
                status_code=409,
                detail="A native directory picker is already open",
            )
        try:
            return {
                "path": choose_native_directory(
                    request.path,
                    title=(
                        "选择资源目录"
                        if request.target == "project"
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

    return router
