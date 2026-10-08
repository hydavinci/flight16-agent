from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
PositiveNumber = Annotated[FiniteNumber, Field(gt=0)]
Nonempty = Annotated[str, Field(min_length=1)]
Status = Literal["pass", "fail", "undetermined", "not_applicable"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Parameters(Contract):
    schema_version: Literal["1.0"]
    aircraft_type: Literal["demo_fixed_wing"]
    condition: Literal["demo_cruise"]
    mass_kg: PositiveNumber
    wing_area_m2: PositiveNumber
    airspeed_mps: PositiveNumber
    air_density_kg_m3: PositiveNumber
    thrust_n: Annotated[FiniteNumber, Field(ge=0)]


class Metric(Contract):
    value: FiniteNumber
    unit: Nonempty


class AnalysisResult(Contract):
    schema_version: Literal["1.0"]
    aircraft_type: Nonempty
    condition: Nonempty
    model_version: Nonempty
    matlab_version: Nonempty
    valid: Annotated[bool, Field(strict=True)]
    diagnostics: list[str]
    metrics: dict[str, Metric]


def validate_result_context(result: AnalysisResult, parameters: Parameters) -> None:
    for field in ("aircraft_type", "condition"):
        if getattr(result, field) != getattr(parameters, field):
            raise ValueError(f"MATLAB result {field} does not match the submitted parameters")


def validate_source_id(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(char in value for char in "[]\n\r")
        or path.suffix != ".md"
    ):
        raise ValueError(
            "Source ID must be a relative Markdown path inside the knowledge directory"
        )
    return value


class Rule(Contract):
    id: Nonempty
    metric: Nonempty
    operator: Literal["<=", ">=", "<", ">"]
    threshold: FiniteNumber
    unit: Nonempty
    aircraft_type: Nonempty
    condition: Nonempty
    source_id: Nonempty
    source_section: Nonempty

    _source_id = field_validator("source_id")(validate_source_id)


class RuleSet(Contract):
    version: Nonempty
    model_version: Nonempty
    demo_only: Literal[True]
    rules: Annotated[list[Rule], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule IDs must be unique")
        return self


class Check(Contract):
    rule_id: str
    metric: str
    status: Status
    value: float | None
    operator: str
    threshold: float
    unit: str
    source_id: str
    source_section: str
    reason: str


class Assessment(Contract):
    rule_version: str
    demo_only: Literal[True] = True
    status: Status
    total: int
    applicable: int
    evaluated: int
    checks: list[Check]


class Evidence(Contract):
    source_id: str
    content: str
    sha256: str
