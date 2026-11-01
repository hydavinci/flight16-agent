import pytest
from pydantic import ValidationError

from flight_agent.runtime.planning_contracts import CopilotDiagnostics, SAFE_SUMMARIES


def test_diagnostics_allow_only_six_safe_fields():
    diagnostic = CopilotDiagnostics(
        category="transport", redacted_request_id=None, exit_code=7,
        elapsed_seconds=0.25, response_bytes=12, summary=SAFE_SUMMARIES["transport"],
    )
    assert set(diagnostic.model_dump()) == {
        "category", "redacted_request_id", "exit_code",
        "elapsed_seconds", "response_bytes", "summary",
    }
    for extra in ("prompt", "stderr", "environment", "credentials", "stdout"):
        with pytest.raises(ValidationError):
            CopilotDiagnostics.model_validate({**diagnostic.model_dump(), extra: "private"})
    with pytest.raises(ValidationError):
        CopilotDiagnostics.model_validate({
            **diagnostic.model_dump(), "summary": "token=private-provider-text",
        })
    with pytest.raises(ValidationError):
        CopilotDiagnostics.model_validate({
            **diagnostic.model_dump(), "redacted_request_id": "raw-provider-id",
        })
