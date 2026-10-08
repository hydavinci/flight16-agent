import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .jobs import JobStore
from .upload_models import MAX_BODY


def install_validation_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "detail": [
                    {"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
                    for error in exc.errors()
                ]
            },
            status_code=422,
        )


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                await JSONResponse({"detail": "Request exceeds 2 MB limit"}, status_code=413)(
                    scope, receive, send
                )
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def bearer_guard(token: str) -> Callable[[str | None], None]:
    if len(token) < 32 or not token.isascii():
        raise ValueError("Worker token must contain at least 32 ASCII characters")

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), f"Bearer {token}".encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid worker authorization")

    return authorize


def artifact_response(store: JobStore, job_id: str, name: str) -> FileResponse:
    try:
        record = store.get(job_id)
        if name not in record.figures or record.status != "completed":
            raise KeyError(name)
        path = store.directory(job_id) / name
        if not path.is_file() or path.is_symlink() or path.parent != store.directory(job_id):
            raise KeyError(name)
        return FileResponse(
            path, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"}
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown artifact") from exc


def safe_log(directory: Path) -> str:
    path = directory / "matlab.log"
    if not path.is_file() or path.is_symlink():
        return ""
    with path.open("rb") as stream:
        return stream.read(1_000_000).decode("utf-8", errors="replace")
