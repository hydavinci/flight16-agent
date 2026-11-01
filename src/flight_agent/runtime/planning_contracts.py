from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .resources import AgentContract

CopilotFailureCategory = Literal[
    "authentication", "authorization", "transport", "timeout",
    "invalid_response", "process_failure",
]

SAFE_SUMMARIES: dict[CopilotFailureCategory, str] = {
    "authentication": "Copilot CLI authentication unavailable; check isolated login access.",
    "authorization": "Copilot CLI account or model access denied; not retried.",
    "transport": "Copilot CLI connection, proxy, DNS, or WebSocket failure; not retried.",
    "timeout": "Copilot CLI timed out; not retried.",
    "invalid_response": (
        "Copilot CLI returned invalid JSON, tool activity, or an incomplete plan/schema response."
    ),
    "process_failure": "Copilot CLI process failed; not retried.",
}


class PlanningMetrics(AgentContract):
    serialized_bytes: Annotated[int, Field(strict=True, ge=0)] = 0
    selected_capability_count: Annotated[int, Field(strict=True, ge=0)] = 0
    selected_source_excerpt_count: Annotated[int, Field(strict=True, ge=0)] = 0
    planning_duration_seconds: Annotated[
        float, Field(ge=0, allow_inf_nan=False)
    ] = 0.0
    correction_round_count: Annotated[int, Field(strict=True, ge=0, le=1)] = 0


class CopilotDiagnostics(AgentContract):
    category: CopilotFailureCategory
    redacted_request_id: Annotated[
        str, Field(pattern=r"^sha256:[a-f0-9]{16}$")
    ] | None
    exit_code: Annotated[int, Field(strict=True)] | None
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    response_bytes: Annotated[int, Field(strict=True, ge=0)]
    summary: Annotated[str, Field(min_length=1, max_length=320)]

    @model_validator(mode="after")
    def allowlisted_summary(self) -> Self:
        if self.summary != SAFE_SUMMARIES[self.category]:
            raise ValueError("Diagnostics require the category's fixed safe summary")
        return self


class PlanningTelemetry(AgentContract):
    metrics: PlanningMetrics
    failure: CopilotDiagnostics | None = None
