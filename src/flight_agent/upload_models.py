import json
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

MAX_SOURCE = 1_000_000
MAX_BODY = 2_000_000
MAX_RESULT = 4_000_000
MAX_LOG = 1_000_000
MAX_FIGURE = 2_000_000
MAX_FIGURES = 5
# JSON escaping can expand each raw log byte into a six-byte Unicode escape.
MAX_RESPONSE = MAX_RESULT + 6 * MAX_LOG + 100_000
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
FIGURE_NAME = re.compile(r"^figure-[1-5]\.png$")
RESERVED = set(
    "break case catch classdef continue else elseif end for function global if otherwise "
    "parfor persistent return spmd switch try while".split()
)


def finite_json(value: JsonValue) -> JsonValue:
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (ValueError, RecursionError) as exc:
        raise ValueError("JSON must contain finite numbers and bounded nesting") from exc
    return value


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    source: str
    kind: Literal["script", "function"]
    parameters: dict[str, JsonValue]
    trusted: Annotated[bool, Field(strict=True)]
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 120

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}\.m", value)
            or value[:-2] in RESERVED
            or value.startswith("flight_agent_")
        ):
            raise ValueError("Use a simple nonreserved MATLAB identifier with the .m extension")
        return value

    @field_validator("source")
    @classmethod
    def bounded_source(cls, value: str) -> str:
        if not value.strip() or "\0" in value or len(value.encode("utf-8")) > MAX_SOURCE:
            raise ValueError("Source must be nonempty UTF-8 text without NUL, at most 1 MB")
        return value

    @field_validator("parameters")
    @classmethod
    def valid_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        finite_json(value)
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 100_000:
            raise ValueError("Parameters exceed the 100 KB limit")
        return value

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        if not self.trusted:
            raise ValueError("Explicit trusted-code consent is required")
        if self.kind == "function":
            pattern = (
                rf"(?m)^\s*function\s+result\s*=\s*{re.escape(self.filename[:-2])}\s*\(params\)"
            )
            if not re.search(pattern, self.source):
                raise ValueError("Expected function result = filename_stem(params)")
        return self


class ExecutionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: JsonValue
    matlab_version: Annotated[str, Field(min_length=1, max_length=200)]
    log: str = ""
    figures: Annotated[list[str], Field(max_length=MAX_FIGURES)] = []

    @model_validator(mode="after")
    def bounded_output(self) -> Self:
        finite_json(self.result)
        if len(json.dumps(self.result, ensure_ascii=False).encode("utf-8")) > MAX_RESULT:
            raise ValueError("Result exceeds the output limit")
        if len(self.log.encode("utf-8")) > MAX_LOG:
            raise ValueError("Log exceeds the output limit")
        if any(not FIGURE_NAME.fullmatch(name) for name in self.figures):
            raise ValueError("Invalid figure name")
        if len(set(self.figures)) != len(self.figures):
            raise ValueError("Duplicate figure names")
        return self


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    status: Literal["queued", "running", "completed", "failed", "interrupted"] = "queued"
    filename: str
    kind: Literal["script", "function"]
    created_at: str
    result: JsonValue = None
    matlab_version: str | None = None
    log: str = ""
    figures: list[str] = []
    error: str | None = None
    source_sha256: str
    remote_id: str | None = None
    agent_status: Literal["not_requested", "running", "completed", "failed", "undetermined"] = (
        "not_requested"
    )
    agent_error: str | None = None
    agent_run_id: str | None = None
    report: str | None = None
    assessment: dict[str, JsonValue] | None = None
