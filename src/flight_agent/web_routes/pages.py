from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

STATIC_ASSETS = {
    "agent.js": "text/javascript",
    "api.js": "text/javascript",
    "state.js": "text/javascript",
    "render.js": "text/javascript",
    "task-evidence.js": "text/javascript",
    "resize-preferences.js": "text/javascript",
    "bootstrap.js": "text/javascript",
    "agent.css": "text/css",
    "matlab-workbench.js": "text/javascript",
    "matlab-workbench.css": "text/css",
    "favicon.svg": "image/svg+xml",
}


def pages_router(static: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "agent.html", media_type="text/html")

    @router.get("/workbench")
    def workbench() -> FileResponse:
        return FileResponse(static / "matlab-workbench.html", media_type="text/html")

    @router.get("/static/{name}")
    def asset(name: str) -> FileResponse:
        if name not in STATIC_ASSETS:
            raise HTTPException(status_code=404, detail="Unknown asset")
        return FileResponse(static / name, media_type=STATIC_ASSETS[name])

    return router
