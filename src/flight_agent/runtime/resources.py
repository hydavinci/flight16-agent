from typing import Annotated, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from ..models import Contract
from .contracts import Digest, Name, Version, fingerprint

Text = Annotated[str, Field(min_length=1, max_length=6000)]


def bounded_json(value: object, depth: int = 0) -> int:
    if depth > 20:
        raise ValueError("JSON exceeds its nesting limit")
    size = 8
    if isinstance(value, str):
        if len(value) > 6000:
            raise ValueError("JSON text exceeds its size limit")
        return len(value.encode("utf-8")) + 2
    if isinstance(value, (list, dict)):
        if len(value) > 10000:
            raise ValueError("JSON container exceeds its size limit")
        if isinstance(value, dict):
            for key in value:
                if len(key) > 200:
                    raise ValueError("JSON key exceeds its size limit")
                size += len(key.encode("utf-8")) + 3
        for item in value.values() if isinstance(value, dict) else value:
            size += bounded_json(item, depth + 1) + 1
            if size > 4_000_000:
                raise ValueError("JSON payload exceeds its size limit")
    return size


class AgentContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @model_validator(mode="after")
    def bounded_payload(self) -> Self:
        value = self.model_dump(mode="json")
        bounded_json(value)
        fingerprint(value)
        return self


class ResourceReference(AgentContract):
    id: Name
    kind: Name
    location: Name
    locator: Text
    fingerprint: Digest
    version: Version | None = None
    metadata: dict[str, JsonValue] = {}
