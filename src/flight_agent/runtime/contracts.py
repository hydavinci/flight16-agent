import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract, Nonempty

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RunId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Version = Annotated[str, Field(min_length=1, max_length=100)]
Effect = Literal[
    "read_only", "execute_project", "write_delivery", "write_candidate", "adopt_candidate"
]


def fingerprint(value: object) -> str:
    try:
        content = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Fingerprint inputs must contain only finite JSON values") from exc
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def finite_mapping(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    fingerprint(value)
    return value


class DesignReference(Contract):
    id: Name
    version: Version
    sha256: Digest


class DesignContext(Contract):
    controller: DesignReference
    plant: DesignReference
    candidate: DesignReference | None = None


class CandidateScope(Contract):
    candidate_id: Name
    root: Nonempty
    source_roots: Annotated[list[Nonempty], Field(min_length=1)]

    @model_validator(mode="after")
    def isolated_workspace(self) -> Self:
        paths = [Path(self.root), *(Path(root) for root in self.source_roots)]
        for path in paths:
            if not path.is_absolute():
                raise ValueError("Candidate and source roots must be absolute")
            if any(item.is_symlink() for item in (path, *path.parents)):
                raise ValueError("Candidate and source roots cannot use symlink aliases")
        candidate, *sources = [path.resolve() for path in paths]
        if any(
            candidate.is_relative_to(source) or source.is_relative_to(candidate)
            for source in sources
        ):
            raise ValueError("Candidate workspace must be isolated from every source root")
        return self


class AdoptionApproval(Contract):
    candidate_id: Name
    baseline_version: Annotated[int, Field(strict=True, ge=1)]


class ExecutionScope(Contract):
    baseline_version: Annotated[int, Field(strict=True, ge=1)]
    confirmed: Annotated[bool, Field(strict=True)]
    design: DesignContext
    allowed_effects: tuple[Effect, ...] = ("read_only",)
    candidate: CandidateScope | None = None
    adoption: AdoptionApproval | None = None

    @model_validator(mode="after")
    def matched_candidate(self) -> Self:
        if self.candidate is not None and (
            self.design.candidate is None
            or self.candidate.candidate_id != self.design.candidate.id
        ):
            raise ValueError("Candidate scope must match the recorded candidate design")
        if self.adoption is not None:
            if self.adoption.baseline_version != self.baseline_version:
                raise ValueError("Adoption approval must match the confirmed baseline version")
            if (
                self.candidate is None
                or self.adoption.candidate_id != self.candidate.candidate_id
            ):
                raise ValueError("Adoption approval must match the isolated candidate")
        return self

    def authorize(self, effect: Effect) -> None:
        if not self.confirmed:
            raise ValueError("Capability execution requires a confirmed task baseline")
        if effect not in self.allowed_effects:
            raise ValueError(f"Effect is outside the confirmed permissions: {effect}")
        if effect in ("write_candidate", "adopt_candidate") and self.candidate is None:
            raise ValueError("Candidate effects require an isolated candidate scope")
        if effect == "adopt_candidate" and self.adoption is None:
            raise ValueError("Candidate adoption requires separate explicit adoption approval")


class ResourceBudget(Contract):
    max_rounds: Annotated[int, Field(strict=True, ge=1, le=20)] = 6
    max_calls: Annotated[int, Field(strict=True, ge=1, le=40)] = 12
    max_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 300
    max_no_progress: Annotated[int, Field(strict=True, ge=1, le=5)] = 2


class AnalysisSpec(Contract):
    capability: Name
    capability_version: Version
    design: DesignContext
    inputs: dict[str, JsonValue]
    data_fingerprints: dict[str, Digest] = {}
    conditions: dict[str, JsonValue] = {}
    numerics: dict[str, JsonValue] = {}

    _finite = field_validator("inputs", "conditions", "numerics")(finite_mapping)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class EvidenceFile(Contract):
    path: Nonempty
    sha256: Digest
    size: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("path")
    @classmethod
    def contained_path(cls, value: str) -> str:
        if (
            PurePosixPath(value).is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(char in value for char in ("\\", ":", "\0", "\r", "\n"))
        ):
            raise ValueError("Evidence requires a relative path without traversal")
        return value


class AnalysisRun(Contract):
    id: RunId
    spec: AnalysisSpec
    output: dict[str, JsonValue]
    output_sha256: Digest
    started_at: Nonempty
    finished_at: Nonempty
    artifacts: list[EvidenceFile] = []
    execution_id: RunId | None = None

    @model_validator(mode="after")
    def verified_output(self) -> Self:
        if fingerprint(self.output) != self.output_sha256:
            raise ValueError("Analysis output does not match its recorded digest")
        return self


class PlanRevision(Contract):
    version: Annotated[int, Field(strict=True, ge=1)]
    baseline_version: Annotated[int, Field(strict=True, ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    analysis_specs: Annotated[list[AnalysisSpec], Field(max_length=8)]
    evidence: Annotated[list[str], Field(max_length=80)] = []
