import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI

from .background import BackgroundRunner
from .service import (
    BodyLimitMiddleware,
    bearer_guard,
    install_validation_handler,
)
from .verification.workbench_worker import workbench_routes
from .verification.worker import project_routes


def create_worker_app(root: Path, token: str) -> FastAPI:
    if os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") != "1":
        raise ValueError(
            "Set MATLAB_ALLOW_TRUSTED_PROJECTS=1 for explicitly trusted code only"
        )
    authorize = bearer_guard(token)
    runner = BackgroundRunner()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        runner.close()

    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware)
    install_validation_handler(app)
    app.include_router(project_routes(root, runner))
    app.include_router(workbench_routes(root, runner))

    @app.get("/health")
    def health() -> dict[str, object]:
        from .matlab import engine_available

        return {
            "service": "matlab-worker",
            "engine_package": engine_available(),
            "trusted_code_only": True,
            "project_execution": True,
            "single_file_execution": True,
        }

    return app
