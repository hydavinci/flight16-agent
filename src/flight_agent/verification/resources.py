from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from ..directories import DirectoryListing
from ..runtime.capability_metadata import Availability
from ..runtime.contracts import Name
from ..runtime.resources import AgentContract, ResourceReference, Text
from .intake import safe_directory

_NAME = TypeAdapter(Name)


@dataclass(frozen=True)
class _Verifier:
    verify: Callable[[ResourceReference], None]
    local: bool


class ResourceLocation(AgentContract):
    id: Name
    display_name: Text
    kind: Name
    location: Name
    availability: Availability


@dataclass(frozen=True)
class ResourceAdapter:
    name: str
    kind: str
    location: str
    inspect: Callable[[str], ResourceReference]
    verify: Callable[[ResourceReference], None]
    browse: Callable[[str], DirectoryListing]
    display_name: str = ""
    availability: Availability = Availability(state="available")
    local: bool = False


class ResourceRegistry:
    def __init__(self) -> None:
        self._verifiers: dict[tuple[str, str], _Verifier] = {}
        self._adapters: dict[str, ResourceAdapter] = {}

    def register(
        self,
        kind: str | ResourceAdapter,
        location: str | None = None,
        verifier: Callable[[ResourceReference], None] | None = None,
        *,
        local: bool = False,
    ) -> None:
        if isinstance(kind, ResourceAdapter):
            adapter = kind
            if location is not None or verifier is not None or local:
                raise ValueError("Resource adapters cannot override their declared verifier")
            name = _NAME.validate_python(adapter.name)
            if name in self._adapters:
                raise ValueError(f"Resource adapter is already registered: {name}")
            ResourceLocation(
                id=name,
                display_name=adapter.display_name or name,
                kind=adapter.kind,
                location=adapter.location,
                availability=adapter.availability,
            )
            self.register(adapter.kind, adapter.location, adapter.verify, local=adapter.local)
            self._adapters[name] = adapter
            return
        if location is None or verifier is None:
            raise ValueError("Resource registration requires a location and verifier")
        key = (_NAME.validate_python(kind), _NAME.validate_python(location))
        if key in self._verifiers:
            raise ValueError(f"Resource verifier is already registered: {kind}/{location}")
        self._verifiers[key] = _Verifier(verifier, local)

    def _adapter(self, name: str) -> ResourceAdapter:
        if name not in self._adapters:
            raise ValueError(f"Resource adapter is not registered: {name}")
        adapter = self._adapters[name]
        if adapter.availability.state != "available":
            raise ValueError(adapter.availability.reason)
        return adapter

    def locations(self) -> list[ResourceLocation]:
        return [
            ResourceLocation(
                id=adapter.name,
                display_name=adapter.display_name or adapter.name,
                kind=adapter.kind,
                location=adapter.location,
                availability=adapter.availability,
            )
            for adapter in self._adapters.values()
        ]

    def inspect(self, adapter_name: str, locator: str) -> ResourceReference:
        adapter = self._adapter(adapter_name)
        resource = ResourceReference.model_validate(adapter.inspect(locator))
        if (resource.kind, resource.location) != (adapter.kind, adapter.location):
            raise ValueError("Inspected resource differs from its registered kind/location")
        self.verify([resource])
        return resource

    def browse(self, adapter_name: str, locator: str = "") -> DirectoryListing:
        return DirectoryListing.model_validate(self._adapter(adapter_name).browse(locator))

    def _entry(self, resource: ResourceReference) -> _Verifier:
        key = (resource.kind, resource.location)
        if key not in self._verifiers:
            raise ValueError(
                f"Resource verifier is not registered: {resource.kind}/{resource.location}"
            )
        return self._verifiers[key]

    def verify(self, resources: list[ResourceReference]) -> None:
        validated = [ResourceReference.model_validate(resource) for resource in resources]
        if len({resource.id for resource in validated}) != len(validated):
            raise ValueError("Resource IDs must be unique")
        for resource in validated:
            self._entry(resource).verify(resource)

    def local_roots(self, resources: list[ResourceReference]) -> list[Path]:
        roots = []
        for resource in resources:
            if self._entry(resource).local:
                path = Path(resource.locator)
                root = path if path.is_dir() else path.parent
                safe_directory(root)
                if path.is_symlink():
                    raise ValueError("Resource source cannot use a symlink")
                roots.append(root)
        return roots
