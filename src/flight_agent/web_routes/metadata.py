from collections.abc import Callable

from fastapi import APIRouter, Depends, Request

from ..verification.coordinator import EngineeringRuntime
from ..verification.resources import ResourceLocation


def metadata_router(
    runtime: EngineeringRuntime,
    authorize: Callable[[Request], None],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorize)])

    @router.get("/api/conversations/capabilities")
    def capabilities() -> list[dict[str, object]]:
        return runtime.capability_catalog()

    @router.get("/api/resource-locations")
    def resource_locations() -> list[ResourceLocation]:
        return runtime.resources.locations()

    return router
