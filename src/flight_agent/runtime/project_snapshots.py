import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .contracts import Digest
from .resources import AgentContract

MAX_SNAPSHOT_FILES = 5000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


class SnapshotFile(AgentContract):
    path: Annotated[str, Field(min_length=1, max_length=6000)]
    sha256: Digest
    size: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if (
            PurePosixPath(value).is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(char in value for char in ("\\", ":", "\0", "\r", "\n"))
        ):
            raise ValueError("Snapshot requires a relative path without traversal")
        return value


class SnapshotManifest(AgentContract):
    schema_version: Literal["1"] = "1"
    files: Annotated[list[SnapshotFile], Field(max_length=MAX_SNAPSHOT_FILES)]

    @model_validator(mode="after")
    def bounded_inventory(self) -> Self:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("Snapshot paths must be unique and sorted")
        if sum(item.size for item in self.files) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Snapshot exceeds the 512 MB limit")
        path_set = set(paths)
        if any(
            parent.as_posix() in path_set
            for path in paths
            for parent in PurePosixPath(path).parents
            if parent != PurePosixPath(".")
        ):
            raise ValueError("Snapshot path is both a file and directory")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ProjectSnapshot(AgentContract):
    snapshot_id: Digest
    source_fingerprint: Digest
    manifest: SnapshotManifest

    @model_validator(mode="after")
    def verified_identity(self) -> Self:
        if self.snapshot_id != self.manifest.snapshot_id:
            raise ValueError("Snapshot identity differs from its manifest")
        return self
