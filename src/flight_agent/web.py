import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from .application import build_application
from .config import SchedulerSettings
from .directories import choose_native_directory
from .service import BodyLimitMiddleware, install_validation_handler
from .verification.coordinator import EngineeringRuntime
from .verification.routes import ConversationManager, verification_router
from .web_routes.artifacts import artifact_router
from .web_routes.configuration import configuration_router
from .web_routes.metadata import metadata_router
from .web_routes.pages import pages_router
from .web_routes.workbench import workbench_router


def create_web_app(
    root: Path,
    *,
    engineering_runtime: EngineeringRuntime | None = None,
    native_directory_picker: bool = False,
    scheduler_settings: SchedulerSettings | None = None,
    conversation_manager: ConversationManager | None = None,
) -> FastAPI:
    services = build_application(
        root,
        engineering_runtime=engineering_runtime,
        scheduler_settings=scheduler_settings,
        conversation_manager=conversation_manager,
    )
    runtime = services.runtime
    manager = services.manager
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            services.close()

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if (
            origin is not None
            and origin != expected
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise HTTPException(
                status_code=403,
                detail="Cross-origin requests are not allowed",
            )

    def authorize(request: Request) -> None:
        supplied = request.headers.get("x-flight-token", "")
        if not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(
                status_code=403,
                detail="Reload this page to obtain its API token",
            )

    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(same_origin)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )
    install_validation_handler(app)
    app.state.services = services
    app.state.conversation_manager = manager
    app.include_router(pages_router(Path(__file__).parent / "static"))
    app.include_router(workbench_router(token, authorize))
    app.include_router(
        configuration_router(
            services,
            token,
            native_directory_picker,
            choose_native_directory,
            authorize,
        )
    )
    app.include_router(metadata_router(runtime, authorize))
    app.include_router(artifact_router(runtime, authorize))
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

    return app
