import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from .models import Contract, finite_json
from .runtime.contracts import Digest, RunId

MAX_WORKBENCH_SOURCE_BYTES = 1_000_000
MAX_WORKBENCH_PARAMETERS_BYTES = 100_000
MAX_WORKBENCH_RESULT_BYTES = 4_000_000
MAX_WORKBENCH_LOG_BYTES = 1_000_000
MAX_WORKBENCH_FIGURE_BYTES = 2_000_000
MAX_WORKBENCH_FIGURES = 5

_MATLAB_FILENAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,62}\.m")
_WORKBENCH_ARTIFACT = re.compile(r"figure-[1-5]\.png")
_MATLAB_KEYWORDS = {
    "arguments",
    "break",
    "case",
    "catch",
    "classdef",
    "continue",
    "else",
    "elseif",
    "end",
    "enumeration",
    "events",
    "for",
    "function",
    "global",
    "if",
    "methods",
    "otherwise",
    "parfor",
    "persistent",
    "properties",
    "return",
    "spmd",
    "switch",
    "try",
    "while",
}


def _utf8_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Text must contain valid UTF-8") from exc


def _bounded_json_bytes(value: JsonValue, limit: int, label: str) -> None:
    finite_json(value)
    try:
        size = len(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError(f"{label} must be valid bounded JSON") from exc
    if size > limit:
        raise ValueError(f"{label} exceeds its byte limit")


def _safe_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(char in value for char in ("\\", ":", "\0", "\r", "\n"))
    ):
        raise ValueError("Artifact path must be relative and without traversal")
    if path.suffix.lower() != ".png":
        raise ValueError("Figure artifact name must end in .png")
    return value


def workbench_artifact_path(value: str) -> str:
    _safe_artifact_path(value)
    if _WORKBENCH_ARTIFACT.fullmatch(value) is None:
        raise ValueError("Workbench artifact path must match figure-N.png")
    return value


def _matlab_code_without_comments(source: str) -> str:
    code = []
    in_block_comment = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped == "%{":
            in_block_comment = True
        elif stripped == "%}":
            in_block_comment = False
        elif not in_block_comment and not line.lstrip().startswith("%"):
            code.append(line)
    return "\n".join(code)


class WorkbenchRunRequest(Contract):
    id: RunId
    filename: str
    source: str
    kind: Literal["script", "function"]
    parameters: dict[str, JsonValue]
    trusted: Annotated[bool, Field(strict=True)]
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 120

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        if not _MATLAB_FILENAME.fullmatch(value):
            raise ValueError("Filename must match a simple MATLAB identifier plus .m")
        stem = value[:-2]
        if stem.lower() in _MATLAB_KEYWORDS:
            raise ValueError("Filename must not use a MATLAB keyword")
        if stem.lower().startswith("flight_agent_"):
            raise ValueError("Filename must not use the flight_agent_ reserved prefix")
        return value

    @field_validator("source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if not value:
            raise ValueError("MATLAB source must be nonempty")
        if "\0" in value:
            raise ValueError("MATLAB source must not contain NUL")
        if len(_utf8_bytes(value)) > MAX_WORKBENCH_SOURCE_BYTES:
            raise ValueError("MATLAB source exceeds its byte limit")
        return value

    @field_validator("parameters")
    @classmethod
    def valid_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _bounded_json_bytes(value, MAX_WORKBENCH_PARAMETERS_BYTES, "Parameters")
        return value

    @field_validator("trusted")
    @classmethod
    def trusted_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Explicit trusted MATLAB execution consent is required")
        return value

    @model_validator(mode="after")
    def valid_function_signature(self) -> Self:
        if self.kind == "function":
            stem = self.filename[:-2]
            declaration = re.compile(
                rf"^\s*function\s+(?:\[[^\]]+\]|[A-Za-z][A-Za-z0-9_]*)"
                rf"\s*=\s*{re.escape(stem)}\s*\(\s*params\s*\)",
                re.MULTILINE,
            )
            if declaration.search(_matlab_code_without_comments(self.source)) is None:
                raise ValueError(
                    "function source must declare the matching filename and single params signature"
                )
        return self


class WorkbenchExecutionReceipt(Contract):
    request_sha256: Digest
    result: JsonValue
    matlab_version: Annotated[str, Field(min_length=1, max_length=200)]
    figures: Annotated[list[str], Field(max_length=MAX_WORKBENCH_FIGURES)] = []

    @field_validator("result")
    @classmethod
    def bounded_result(cls, value: JsonValue) -> JsonValue:
        _bounded_json_bytes(value, MAX_WORKBENCH_RESULT_BYTES, "Result")
        return value

    @field_validator("figures")
    @classmethod
    def safe_figures(cls, values: list[str]) -> list[str]:
        for value in values:
            _safe_artifact_path(value)
        if len(set(values)) != len(values):
            raise ValueError("Figure paths must be unique")
        return values


class WorkbenchArtifact(Contract):
    path: str
    sha256: Digest
    size: Annotated[int, Field(strict=True, ge=0, le=MAX_WORKBENCH_FIGURE_BYTES)]
    media_type: Literal["image/png"]
    role: Literal["figure"]

    @field_validator("path")
    @classmethod
    def safe_figure_path(cls, value: str) -> str:
        return _safe_artifact_path(value)


class WorkbenchRunRecord(Contract):
    id: RunId
    request_sha256: Digest
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    stage: str
    created_at: str
    updated_at: str
    filename: str
    kind: Literal["script", "function"]
    matlab_version: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    result: JsonValue | None = None
    error: str | None = None
    log: str = ""
    artifacts: Annotated[list[WorkbenchArtifact], Field(max_length=MAX_WORKBENCH_FIGURES)] = []

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        if not _MATLAB_FILENAME.fullmatch(value):
            raise ValueError("Filename must match a simple MATLAB identifier plus .m")
        stem = value[:-2]
        if stem.lower() in _MATLAB_KEYWORDS:
            raise ValueError("Filename must not use a MATLAB keyword")
        if stem.lower().startswith("flight_agent_"):
            raise ValueError("Filename must not use the flight_agent_ reserved prefix")
        return value

    @field_validator("log")
    @classmethod
    def bounded_log(cls, value: str) -> str:
        if len(_utf8_bytes(value)) > MAX_WORKBENCH_LOG_BYTES:
            raise ValueError("Log exceeds its byte limit")
        return value

    @field_validator("result")
    @classmethod
    def bounded_result(cls, value: JsonValue | None) -> JsonValue | None:
        if value is not None:
            _bounded_json_bytes(value, MAX_WORKBENCH_RESULT_BYTES, "Result")
        return value

    @model_validator(mode="after")
    def terminal_state(self) -> Self:
        if len({item.path for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("Artifact paths must be unique")
        if self.stage in ("completed", "failed", "interrupted") and self.status != self.stage:
            raise ValueError("Terminal stage must match terminal status")

        if self.status == "completed":
            if self.stage != "completed":
                raise ValueError("A completed run requires the completed execution stage")
            if self.matlab_version is None:
                raise ValueError("A completed run requires a MATLAB version")
            if self.error is not None:
                raise ValueError("A completed run cannot contain an error")
            return self

        if self.matlab_version is not None:
            raise ValueError("MATLAB version is permitted only for completed runs")
        if self.result is not None:
            raise ValueError("Result output is permitted only for completed runs")
        if self.artifacts:
            raise ValueError("Artifacts are permitted only for completed runs")
        if self.status in ("failed", "interrupted") and not self.error:
            raise ValueError("Unsuccessful runs require explicit error text")
        if self.status in ("failed", "interrupted") and self.stage != self.status:
            raise ValueError("Unsuccessful run stage must match terminal status")
        if self.status in ("queued", "running") and self.error is not None:
            raise ValueError("Queued or running runs cannot contain an error")
        return self


def workbench_request_digest(request: WorkbenchRunRequest) -> str:
    content = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
