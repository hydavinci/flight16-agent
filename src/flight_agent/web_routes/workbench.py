import warnings
from collections.abc import Callable
from importlib.resources import files
from io import BytesIO

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from PIL import Image
from pydantic import ValidationError

from ..remote import RemoteClient, RemoteError, RemoteHTTPError
from ..workbench_contracts import (
    WorkbenchRunRecord,
    WorkbenchRunRequest,
    workbench_artifact_path,
)

EXAMPLES = {"web_script.m", "web_function.m"}
_UNAVAILABLE = "MATLAB Worker is unavailable"


def _upstream_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RemoteHTTPError) and exc.status_code in (404, 409):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    return HTTPException(status_code=503, detail=_UNAVAILABLE)


def _verified_png(content: bytes) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format != "PNG" or image.width * image.height > 16_000_000:
                    raise ValueError("Invalid PNG image")
                image.verify()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError("Invalid PNG image") from exc
    return content


def workbench_router(
    token: str,
    authorize: Callable[[Request], None],
    remote_factory: Callable[[], RemoteClient] = RemoteClient.from_environment,
) -> APIRouter:
    router = APIRouter(prefix="/api/workbench")
    auth = [Depends(authorize)]

    def remote() -> RemoteClient:
        try:
            return remote_factory()
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status_code=503,
                detail="MATLAB Worker is not configured",
            ) from exc

    @router.get("/bootstrap")
    def bootstrap() -> dict[str, object]:
        error = None
        try:
            remote_factory()
        except (ValueError, OSError) as exc:
            error = str(exc)
        return {
            "token": token,
            "worker_configured": error is None,
            "worker_error": error,
        }

    @router.get("/examples/{name}", dependencies=auth)
    def example(name: str) -> Response:
        if name not in EXAMPLES:
            raise HTTPException(status_code=404, detail="Unknown example")
        try:
            content = files("flight_agent").joinpath("examples", name).read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(
                status_code=404,
                detail="Example missing from application resources",
            ) from exc
        return Response(
            content=content,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @router.get("/runs", dependencies=auth)
    def recent() -> list[WorkbenchRunRecord]:
        try:
            return remote().recent_workbench()
        except (httpx.HTTPError, RemoteError, ValidationError, ValueError, OSError) as exc:
            raise _upstream_error(exc) from exc

    @router.post("/runs", status_code=202, dependencies=auth)
    def submit(request: WorkbenchRunRequest) -> WorkbenchRunRecord:
        try:
            return remote().submit_workbench(request)
        except (httpx.HTTPError, RemoteError, ValidationError, ValueError, OSError) as exc:
            raise _upstream_error(exc) from exc

    @router.get("/runs/{run_id}", dependencies=auth)
    def status(run_id: str) -> WorkbenchRunRecord:
        try:
            return remote().workbench_status(run_id)
        except (httpx.HTTPError, RemoteError, ValidationError, ValueError, OSError) as exc:
            raise _upstream_error(exc) from exc

    @router.get("/runs/{run_id}/artifacts/{name:path}", dependencies=auth)
    def artifact(run_id: str, name: str) -> Response:
        try:
            name = workbench_artifact_path(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unknown artifact") from exc
        try:
            content = _verified_png(remote().workbench_artifact(run_id, name))
        except (httpx.HTTPError, RemoteError, ValidationError, ValueError, OSError) as exc:
            raise _upstream_error(exc) from exc
        return Response(
            content=content,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return router
