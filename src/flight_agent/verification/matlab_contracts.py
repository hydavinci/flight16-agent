import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, ValidationInfo, field_validator, model_validator

from ..models import Contract, FiniteNumber, Nonempty
from ..runtime.contracts import Digest, RunId
from ..runtime.observations import MetricValue, OutputArtifact
from ..upload_models import MAX_RESULT, finite_json
from .contracts import FileDigest, SteadyStatePolicy, matlab_identifier, relative_path
from .intake import safe_directory


def _bounded_json(value: JsonValue, limit: int = 100_000) -> None:
    finite_json(value)
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > limit:
        raise ValueError(f"JSON exceeds the {limit}-byte limit")


def _entry_path(value: str, suffixes: set[str]) -> str:
    relative_path(value)
    path = PurePosixPath(value)
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"Expected an existing {'/'.join(sorted(suffixes))} entry")
    return value


class _MatlabContract(Contract):
    model_config = ConfigDict(strict=True, revalidate_instances="always")


class MatlabEntry(_MatlabContract):
    path: Nonempty
    kind: Literal["script", "function"]
    arguments: Annotated[list[JsonValue], Field(max_length=100)] = []
    signature: Annotated[str, Field(max_length=4096)] | None = None
    evidence: Annotated[list[str], Field(max_length=20)] = []

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _entry_path(value, {".m"})

    @model_validator(mode="after")
    def finite_arguments(self) -> Self:
        _bounded_json(self.arguments)
        if self.kind == "script" and self.arguments:
            raise ValueError("A script cannot accept positional arguments")
        return self


class MatlabProjectDescriptor(_MatlabContract):
    host: Literal["agent"] = "agent"
    root: Nonempty
    fingerprint: Digest
    files: list[FileDigest]
    models: list[str] = []
    entries: list[MatlabEntry] = []
    project_files: list[str] = []
    notes: list[str] = []
    settings: dict[str, JsonValue] = {}

    @field_validator("root")
    @classmethod
    def safe_root(cls, value: str) -> str:
        safe_directory(Path(value))
        return value

    @field_validator("settings")
    @classmethod
    def finite_settings(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value, MAX_RESULT)
        return value

    @model_validator(mode="after")
    def inventoried_candidates(self) -> Self:
        paths = {item.path for item in self.files}
        if len(paths) != len(self.files):
            raise ValueError("Project inventory paths must be unique")
        for path in self.models:
            _entry_path(path, {".slx", ".mdl"})
        for path in self.project_files:
            relative_path(path)
            if PurePosixPath(path).suffix.lower() != ".prj":
                raise ValueError("Expected an existing .prj project file")
        candidates = [*self.models, *self.project_files, *(entry.path for entry in self.entries)]
        if not set(candidates) <= paths or len(candidates) != len(set(candidates)):
            raise ValueError("Candidates must be unique inventoried files")
        return self


class SignalSelection(_MatlabContract):
    source: Literal["logsout", "workspace", "return"]
    path: Annotated[list[str], Field(min_length=1, max_length=32)]
    unit: Nonempty

    @field_validator("path")
    @classmethod
    def explicit_fields(cls, value: list[str]) -> list[str]:
        for name in value:
            matlab_identifier(name)
        return value


class ProjectExecutionInput(_MatlabContract):
    mode: Literal["simulink", "entry"]
    model: str | None = None
    entry: MatlabEntry | None = None
    initializer: MatlabEntry | None = None
    project_file: str | None = None
    working_directory: str | None = None
    input_signal: SignalSelection | None = None
    response_signal: SignalSelection | None = None
    conditions: dict[str, JsonValue] = {}

    @field_validator("model")
    @classmethod
    def safe_model(cls, value: str | None) -> str | None:
        return _entry_path(value, {".slx", ".mdl"}) if value is not None else None

    @field_validator("project_file", "working_directory")
    @classmethod
    def safe_relative(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None:
            relative_path(value)
            if info.field_name == "project_file" and PurePosixPath(value).suffix.lower() != ".prj":
                raise ValueError("Expected an existing .prj project file")
        return value

    @field_validator("conditions")
    @classmethod
    def finite_conditions(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value)
        return value

    @model_validator(mode="after")
    def explicit_execution(self) -> Self:
        if self.mode == "simulink" and (self.model is None or self.entry is not None):
            raise ValueError("Simulink mode requires only an explicit model")
        if self.mode == "entry" and (self.entry is None or self.model is not None):
            raise ValueError("Entry mode requires only an explicit entry")
        if self.model is not None:
            matlab_identifier(PurePosixPath(self.model).stem)
        for entry in (self.entry, self.initializer):
            if entry is not None and entry.kind == "function":
                matlab_identifier(PurePosixPath(entry.path).stem)
        if (self.input_signal is None) != (self.response_signal is None):
            raise ValueError("Input and response selectors must be provided as a pair")
        return self


class StepinfoInput(_MatlabContract):
    series: OutputArtifact
    settling_band: Annotated[FiniteNumber, Field(gt=0, lt=1)]
    steady_state: SteadyStatePolicy = SteadyStatePolicy()
    conditions: dict[str, JsonValue] = {}

    @field_validator("conditions")
    @classmethod
    def finite_conditions(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value)
        return value


class LocalMatlabRequest(_MatlabContract):
    id: RunId
    operation: Literal["execute_project", "stepinfo"]
    inputs: ProjectExecutionInput | StepinfoInput
    project: MatlabProjectDescriptor | None = None
    input_evidence: Annotated[list[OutputArtifact], Field(max_length=200)] = []
    timeout_seconds: Annotated[int, Field(ge=1, le=1800)] = 300

    @model_validator(mode="after")
    def matched_operation(self) -> Self:
        if self.operation == "stepinfo":
            if not isinstance(self.inputs, StepinfoInput) or self.project is not None:
                raise ValueError("stepinfo requires StepinfoInput")
            return self
        if not isinstance(self.inputs, ProjectExecutionInput) or self.project is None:
            raise ValueError("execute_project requires ProjectExecutionInput and a project")
        inputs = self.inputs
        if inputs.model is not None and inputs.model not in self.project.models:
            raise ValueError("Model must be an inventoried candidate")
        if (
            inputs.project_file is not None
            and inputs.project_file not in self.project.project_files
        ):
            raise ValueError("Project file must be an inventoried candidate")
        for entry in (inputs.entry, inputs.initializer):
            if entry is not None and not any(
                (entry.path, entry.kind, entry.signature) == (item.path, item.kind, item.signature)
                for item in self.project.entries
            ):
                raise ValueError("Entry must match a discovered candidate and its signature")
        if inputs.working_directory is not None and not any(
            item.path.startswith(inputs.working_directory + "/") for item in self.project.files
        ):
            raise ValueError("Working directory must contain inventoried project files")
        return self


class LocalMatlabResult(_MatlabContract):
    id: RunId
    request_sha256: Digest
    execution_status: Literal["completed", "failed", "interrupted", "unknown"]
    valid: bool
    details: dict[str, JsonValue] = {}
    diagnostics: Annotated[list[str], Field(max_length=200)] = []
    artifacts: Annotated[list[OutputArtifact], Field(max_length=200)] = []

    @field_validator("details")
    @classmethod
    def finite_details(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value, MAX_RESULT)
        return value

    @model_validator(mode="after")
    def terminal_validity(self) -> Self:
        if self.valid and self.execution_status != "completed":
            raise ValueError("Only a completed execution can be valid")
        if len({item.path for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("Artifact paths must be unique")
        return self


class LocalMatlabCheckpoint(_MatlabContract):
    id: RunId
    request_sha256: Digest
    status: Literal["running", "completed", "failed", "interrupted", "unknown"]
    interpreter: Nonempty
    pid: Annotated[int, Field(gt=0)] | None = None
    worker_exit_confirmed: bool = False
    receipt_sha256: Digest | None = None
    result_sha256: Digest | None = None


class LocalMatlabExecution(_MatlabContract):
    id: RunId
    request_sha256: Digest
    snapshot_root: Nonempty
    output_directory: Nonempty
    inputs: ProjectExecutionInput
    project_paths: list[str]
    entry_nargin: Annotated[int, Field(ge=0, le=100)]
    entry_nargout: Annotated[int, Field(ge=0, le=100)]
    initializer_nargin: Annotated[int, Field(ge=0, le=100)]
    initializer_nargout: Annotated[int, Field(ge=0, le=100)]
    entry_arguments: list[str]
    initializer_arguments: list[str]


class LocalMatlabStepinfoExecution(_MatlabContract):
    id: RunId
    request_sha256: Digest
    snapshot_root: Nonempty
    output_directory: Nonempty
    inputs: StepinfoInput
    series_path: Nonempty


class MatlabRuntimeMetadata(_MatlabContract):
    id: RunId
    request_sha256: Digest
    matlab_version: Nonempty
    matlab_release: Nonempty
    snapshot_root: Nonempty
    working_directory: Nonempty
    products: list[dict[str, JsonValue]] | dict[str, JsonValue]
    settings: dict[str, JsonValue]
    simulation_model_info: dict[str, JsonValue] | None = None
    simulation_timing_info: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def bounded_metadata(self) -> Self:
        _bounded_json(self.model_dump(mode="json"), MAX_RESULT)
        return self


class MatlabExecutionMetadata(MatlabRuntimeMetadata):
    inputs: ProjectExecutionInput | StepinfoInput
    input_evidence: list[OutputArtifact] = []
    conditions: dict[str, JsonValue]
    input_provenance: Literal["canonical_request"]


class MatlabSeries(_MatlabContract):
    time: Annotated[list[FiniteNumber], Field(min_length=2, max_length=100_000)]
    values: Annotated[list[FiniteNumber], Field(min_length=2, max_length=100_000)]
    time_unit: Nonempty
    unit: Nonempty
    name: Nonempty

    @model_validator(mode="after")
    def scalar_ordered_samples(self) -> Self:
        if len(self.time) != len(self.values):
            raise ValueError("Series time and values must have matching lengths")
        if any(right <= left for left, right in zip(self.time, self.time[1:])):
            raise ValueError("Series time must be strictly increasing")
        return self


class MatlabSeriesPair(_MatlabContract):
    input: MatlabSeries
    response: MatlabSeries


class MatlabProjectOutput(_MatlabContract):
    id: RunId
    request_sha256: Digest
    artifact_paths: Annotated[list[Nonempty], Field(max_length=10)]
    output_evidence: bool
    details: dict[str, JsonValue]
    diagnostics: Annotated[list[str], Field(max_length=200)]

    @field_validator("details")
    @classmethod
    def bounded_details(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json(value, MAX_RESULT)
        return value


class MatlabStepinfoOutput(MatlabProjectOutput):
    valid: bool
    metrics: dict[Literal["overshoot", "settling_time"], MetricValue]

    @model_validator(mode="after")
    def measured_metrics(self) -> Self:
        if self.valid:
            if set(self.metrics) != {"overshoot", "settling_time"}:
                raise ValueError("Valid stepinfo requires both actual metrics")
            for name, metric in self.metrics.items():
                unit = "%" if name == "overshoot" else "s"
                if not metric.valid or metric.value is None or metric.unit != unit:
                    raise ValueError("stepinfo requires finite metrics in declared units")
                if metric.value < 0:
                    raise ValueError("stepinfo metrics cannot be negative")
        elif not self.diagnostics or any(metric.valid for metric in self.metrics.values()):
            raise ValueError("Invalid stepinfo requires diagnostics and no valid metrics")
        return self
