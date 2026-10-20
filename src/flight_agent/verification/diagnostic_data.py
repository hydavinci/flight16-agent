"""Bounded, registered JSON evidence; never scans or executes project source."""

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, NoReturn, Self
from uuid import uuid4

from pydantic import Field, JsonValue, field_validator, model_validator

from ..models import Contract, FiniteNumber, Nonempty
from ..runtime.capabilities import CapabilityRegistry
from ..runtime.contracts import (
    DesignContext,
    Digest,
    EvidenceFile,
    Name,
    fingerprint,
    finite_mapping,
)

_MANIFEST = "flight-diagnosis.json"
_MAX_BYTES = 8_000_000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_Samples = Annotated[list[FiniteNumber], Field(min_length=2, max_length=20000)]


def _json_path(value: str) -> str:
    if (
        PurePosixPath(value).is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(char in value for char in ("\\", ":", "\0", "\r", "\n"))
        or PurePosixPath(value).suffix != ".json"
    ):
        raise ValueError("Dataset path must be a safe relative JSON path")
    return value


class DatasetRegistration(Contract):
    id: Name
    path: str
    kind: Literal["time_series", "frequency_response"]
    source: Literal["simulation", "test", "synthetic"]
    parameters: dict[Annotated[str, Field(min_length=1, max_length=64)], FiniteNumber] = Field(
        default_factory=dict, max_length=32,
    )

    _path = field_validator("path")(_json_path)


class DiagnosticManifest(Contract):
    schema_version: Literal["1.0"]
    name: Annotated[Nonempty, Field(max_length=200)]
    design: DesignContext
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    synthetic: Annotated[bool, Field(strict=True)] = False
    datasets: Annotated[list[DatasetRegistration], Field(max_length=16)]

    @field_validator("conditions")
    @classmethod
    def bounded_conditions(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pending: list[tuple[JsonValue, int]] = [(value, 1)]
        while pending:
            item, depth = pending.pop()
            if isinstance(item, (dict, list)):
                if depth > 8:
                    raise ValueError("Conditions JSON nesting must not exceed 8 levels")
                children = item.values() if isinstance(item, dict) else item
                pending.extend((child, depth + 1) for child in children)
        value = finite_mapping(value)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 16_000:
            raise ValueError("Conditions canonical JSON must not exceed 16,000 bytes")
        return value

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [dataset.id for dataset in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("Dataset IDs must be unique")
        return self


class DiagnosticProject(Contract):
    root: str
    fingerprint: Digest
    manifest: DiagnosticManifest
    files: list[EvidenceFile]
    summary: dict[str, JsonValue]

    @field_validator("summary")
    @classmethod
    def bounded_summary(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pending = list(value.values())
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                raise ValueError("Diagnostic summaries cannot contain raw arrays")
            if isinstance(item, dict):
                pending.extend(item.values())
        return finite_mapping(value)


class DiagnosticMetric(Contract):
    value: FiniteNumber
    unit: Annotated[Nonempty, Field(max_length=80)]


_Messages = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=512)]], Field(max_length=64)
]


class DiagnosticOutput(Contract):
    valid: Annotated[bool, Field(strict=True)]
    metrics: dict[Annotated[str, Field(min_length=1, max_length=64)], DiagnosticMetric] = Field(
        max_length=64,
    )
    observations: _Messages
    unknowns: _Messages
    data_fingerprints: dict[str, Digest]


class TimeSeriesSignal(Contract):
    unit: Annotated[Nonempty, Field(max_length=80)]
    values: _Samples


class TimeSeries(Contract):
    schema_version: Literal["1.0"]
    kind: Literal["time_series"]
    time_unit: Literal["s"]
    time: _Samples
    signals: Annotated[dict[Name, TimeSeriesSignal], Field(max_length=32)]

    @model_validator(mode="after")
    def consistent_samples(self) -> Self:
        if any(left >= right for left, right in pairwise(self.time)):
            raise ValueError("Time must be strictly increasing")
        if any(len(signal.values) != len(self.time) for signal in self.signals.values()):
            raise ValueError("Signal and time lengths must match")
        return self


class FrequencyResponse(Contract):
    schema_version: Literal["1.0"]
    kind: Literal["frequency_response"]
    loop: Name
    frequency_hz: _Samples
    gain_db: _Samples
    phase_deg: _Samples
    phase_convention: Literal["unwrapped"]
    feedback_sign: Literal[-1] = -1

    @field_validator("feedback_sign", mode="before")
    @classmethod
    def negative_feedback(cls, value: object) -> object:
        if type(value) is not int or value != -1:
            raise ValueError("feedback_sign must be the integer -1 (negative feedback)")
        return value

    @model_validator(mode="after")
    def consistent_samples(self) -> Self:
        if self.frequency_hz[0] <= 0 or any(
            left >= right for left, right in pairwise(self.frequency_hz)
        ):
            raise ValueError("Frequencies must be positive and strictly increasing")
        if not (len(self.frequency_hz) == len(self.gain_db) == len(self.phase_deg)):
            raise ValueError("Frequency, gain and phase lengths must match")
        return self


def _absolute(path: Path | str) -> Path:
    path = Path(path)
    if ".." in path.parts or path.anchor == "//":
        raise ValueError("Directory paths cannot contain traversal or ambiguous roots")
    return path.absolute()


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    fd = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as exc:
        raise ValueError("Cannot access safe directory/file; symlinks are forbidden") from exc
    finally:
        os.close(fd)


@contextmanager
def _parent(root_fd: int, path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    parts = PurePosixPath(_json_path(path)).parts
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    except OSError as exc:
        raise ValueError(f"Cannot access safe registered path: {path}; no symlinks") from exc
    finally:
        os.close(fd)


def _read_bytes(root_fd: int, path: str) -> bytes:
    with _parent(root_fd, path) as (parent_fd, name):
        fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Registered input must be a regular JSON file: {path}")
            if info.st_size > _MAX_BYTES:
                raise ValueError(f"JSON limit is 8,000,000 bytes (below 8 MiB): {path}")
            content = stream.read(_MAX_BYTES + 1)
            if len(content) > _MAX_BYTES:
                raise ValueError(f"JSON limit is 8,000,000 bytes (below 8 MiB): {path}")
            return content


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite_constant(value: str) -> NoReturn:
    raise ValueError(f"Nonfinite JSON value: {value}")


def _json(content: bytes) -> object:
    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_nonfinite_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def _evidence(path: str, content: bytes) -> EvidenceFile:
    return EvidenceFile(path=path, sha256=hashlib.sha256(content).hexdigest(), size=len(content))


def _dataset(registration: DatasetRegistration, content: bytes) -> TimeSeries | FrequencyResponse:
    value = _json(content)
    if registration.kind == "time_series":
        return TimeSeries.model_validate(value)
    return FrequencyResponse.model_validate(value)


def inspect_diagnostic_project(root: Path | str) -> DiagnosticProject:
    root = _absolute(root)
    with _directory(root) as root_fd:
        content = _read_bytes(root_fd, _MANIFEST)
        manifest = DiagnosticManifest.model_validate(_json(content))
        files = {_MANIFEST: _evidence(_MANIFEST, content)}
        datasets: dict[str, JsonValue] = {}
        for registration in manifest.datasets:
            content = _read_bytes(root_fd, registration.path)
            data = _dataset(registration, content)
            evidence = _evidence(registration.path, content)
            if registration.path in files and files[registration.path] != evidence:
                raise ValueError("Registered input changed during inspection")
            files[registration.path] = evidence
            summary: dict[str, JsonValue] = {
                **registration.model_dump(mode="json"),
                "schema_version": data.schema_version,
                "sha256": evidence.sha256,
            }
            if isinstance(data, TimeSeries):
                summary.update(
                    samples=len(data.time),
                    start=data.time[0],
                    end=data.time[-1],
                    signals={name: {"unit": signal.unit} for name, signal in data.signals.items()},
                )
            else:
                summary.update(
                    samples=len(data.frequency_hz),
                    loop=data.loop,
                    start_hz=data.frequency_hz[0],
                    end_hz=data.frequency_hz[-1],
                    phase_convention=data.phase_convention,
                    feedback_sign=data.feedback_sign,
                )
            datasets[registration.id] = summary
    evidence_files = list(files.values())
    return DiagnosticProject(
        root=str(root),
        fingerprint=fingerprint({
            "manifest": manifest.model_dump(mode="json"),
            "files": [file.model_dump(mode="json") for file in evidence_files],
        }),
        manifest=manifest,
        files=evidence_files,
        summary={
            "schema_version": manifest.schema_version,
            "name": manifest.name,
            "synthetic": manifest.synthetic,
            "design": manifest.design.model_dump(mode="json"),
            "conditions_fingerprint": fingerprint(manifest.conditions),
            "datasets": datasets,
            "files": {
                file.path: {"sha256": file.sha256, "size": file.size} for file in evidence_files
            },
        },
    )


def _checked_bytes(root_fd: int, evidence: EvidenceFile) -> bytes:
    content = _read_bytes(root_fd, evidence.path)
    if _evidence(evidence.path, content) != evidence:
        raise ValueError(f"Registered input digest changed: {evidence.path}")
    return content


def _snapshot_file(root_fd: int, path: str, content: bytes) -> None:
    with _parent(root_fd, path, create=True) as (parent_fd, name):
        directory = os.fstat(parent_fd)
        binding = hashlib.sha256(
            os.fsencode(f"{directory.st_dev}:{directory.st_ino}:{name}\0") + content
        ).hexdigest()
        attempt = uuid4().hex
        staging = f".diagnostic-{attempt}.tmp"
        fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=parent_fd,
        )
        receipt = None
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
            owner = f".diagnostic-{binding}-{info.st_dev}-{info.st_ino}.owner"
            owner_fd = os.open(
                owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                dir_fd=parent_fd,
            )
            receipt = owner
            with os.fdopen(owner_fd, "w") as record:
                record.write(attempt)
                record.flush()
                os.fchmod(record.fileno(), 0o444)
                os.fsync(record.fileno())
            try:
                os.link(
                    staging, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2):
                        raise ValueError("Snapshot files must be safe regular files, not hardlinks")
                    if info.st_size != len(content) or stream.read(_MAX_BYTES + 1) != content:
                        raise ValueError(f"Snapshot already contains different content: {path}")
                    if info.st_nlink == 2:
                        owner = f".diagnostic-{binding}-{info.st_dev}-{info.st_ino}.owner"
                        owner_fd = os.open(owner, _READ_FLAGS, dir_fd=parent_fd)
                        with os.fdopen(owner_fd, "rb") as record:
                            owner_info = os.fstat(record.fileno())
                            if (
                                not stat.S_ISREG(owner_info.st_mode)
                                or owner_info.st_nlink != 1
                                or (owner_info.st_mode | info.st_mode) & 0o222
                            ):
                                raise ValueError("Snapshot staging ownership cannot be verified")
                            token = record.read(33)
                        if len(token) != 32 or any(c not in b"0123456789abcdef" for c in token):
                            raise ValueError("Snapshot staging ownership cannot be verified")
                        orphan = f".diagnostic-{token.decode('ascii')}.tmp"
                        orphan_info = os.stat(orphan, dir_fd=parent_fd, follow_symlinks=False)
                        if (
                            not stat.S_ISREG(orphan_info.st_mode)
                            or orphan_info.st_dev != info.st_dev
                            or orphan_info.st_ino != info.st_ino
                            or orphan_info.st_nlink != 2
                        ):
                            raise ValueError("Snapshot staging ownership cannot be verified")
                        os.unlink(orphan, dir_fd=parent_fd)
                        if os.fstat(stream.fileno()).st_nlink != 1:
                            raise ValueError(
                                "Snapshot files must be safe regular files, not hardlinks"
                            )
                        os.unlink(owner, dir_fd=parent_fd)
                    os.fchmod(stream.fileno(), 0o444)
        finally:
            # Keep the receipt until the staging link is gone, including across process death.
            os.unlink(staging, dir_fd=parent_fd)
            if receipt is not None:
                os.unlink(receipt, dir_fd=parent_fd)


def snapshot_diagnostic_project(project: DiagnosticProject, target: Path) -> DiagnosticProject:
    actual = inspect_diagnostic_project(project.root)
    if actual != project:
        raise ValueError("Diagnostic project identity changed before snapshot")
    source = Path(actual.root)
    target = _absolute(target)
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Snapshot target must be isolated from the source directory")
    with _directory(target.parent) as parent_fd:
        try:
            os.mkdir(target.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    with _directory(source) as source_fd, _directory(target) as target_fd:
        for file in actual.files:
            _snapshot_file(target_fd, file.path, _checked_bytes(source_fd, file))
    result = inspect_diagnostic_project(target)
    if result.fingerprint != actual.fingerprint:
        raise ValueError("Snapshot identity changed while copying")
    return result


@dataclass(frozen=True)
class DiagnosticContext:
    project: DiagnosticProject
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        project = DiagnosticProject.model_validate(self.project.model_dump(mode="json"))
        if inspect_diagnostic_project(project.root) != project:
            raise ValueError("Diagnostic project identity mismatch")
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "_identity", fingerprint(project.model_dump(mode="json")))

    def read_dataset(self, dataset: str) -> TimeSeries | FrequencyResponse:
        if fingerprint(self.project.model_dump(mode="json")) != self._identity:
            raise ValueError("Diagnostic project descriptor identity changed")
        registration = next(
            (item for item in self.project.manifest.datasets if item.id == dataset), None
        )
        if registration is None:
            raise ValueError(f"Dataset is not registered: {dataset}")
        files = {file.path: file for file in self.project.files}
        with _directory(Path(self.project.root)) as root_fd:
            _checked_bytes(root_fd, files[_MANIFEST])
            return _dataset(registration, _checked_bytes(root_fd, files[registration.path]))


def diagnostic_capabilities() -> CapabilityRegistry[DiagnosticContext]:
    from .diagnostic_signals import diagnostic_capabilities as registry

    return registry()
