import json

import pytest
from pydantic import ValidationError

from flight_agent.copilot_diagnostics import (
    CopilotError,
    CopilotInvalidResponseError,
    classify_failure,
    make_diagnostics,
    schema_feedback,
)
from flight_agent.runtime.planning_contracts import SAFE_SUMMARIES, CopilotDiagnostics
from flight_agent.verification.agent_planner import AgentDecision


def test_diagnostics_allow_only_six_safe_fields():
    diagnostic = CopilotDiagnostics(
        category="transport",
        redacted_request_id=None,
        exit_code=7,
        elapsed_seconds=0.25,
        response_bytes=12,
        summary=SAFE_SUMMARIES["transport"],
    )
    assert set(diagnostic.model_dump()) == {
        "category",
        "redacted_request_id",
        "exit_code",
        "elapsed_seconds",
        "response_bytes",
        "summary",
    }
    for extra in ("prompt", "stderr", "environment", "credentials", "stdout"):
        with pytest.raises(ValidationError):
            CopilotDiagnostics.model_validate({**diagnostic.model_dump(), extra: "private"})
    with pytest.raises(ValidationError):
        CopilotDiagnostics.model_validate(
            {
                **diagnostic.model_dump(),
                "summary": "token=private-provider-text",
            }
        )
    with pytest.raises(ValidationError):
        CopilotDiagnostics.model_validate(
            {
                **diagnostic.model_dump(),
                "redacted_request_id": "raw-provider-id",
            }
        )


@pytest.mark.parametrize(
    "text,category",
    [
        ("HTTP 401 Unauthorized", "authentication"),
        ("not logged in", "authentication"),
        ("token has expired", "authentication"),
        ("HTTP 403 Forbidden", "authorization"),
        ("model access denied", "authorization"),
        ("account access denied", "authorization"),
        ("HTTP 407 Proxy Authentication Required", "transport"),
        ("proxy authentication required", "transport"),
        ("WebSocket closed", "transport"),
        ("proxy connection failed", "transport"),
        ("getaddrinfo ENOTFOUND", "transport"),
        ("ECONNRESET", "transport"),
        ("unknown CLI problem", "process_failure"),
    ],
)
def test_failure_categories_are_distinct(text, category):
    assert classify_failure("", text) == category


def test_diagnostic_summary_and_request_id_cannot_echo_secrets():
    raw = (
        "HTTP 401 Unauthorized\nrequest-id: private-request-token\n"
        "Authorization: ******"
        "****** prompt text\n"
    )
    diagnostic = make_diagnostics(
        "authentication",
        stdout="秘密",
        stderr=raw,
        exit_code=7,
        elapsed_seconds=0.125,
    )
    encoded = diagnostic.model_dump_json()
    for private in (
        "private-token",
        "private-password",
        "private prompt",
        "/private/project",
        "private-request-token",
        "秘密",
    ):
        assert private not in encoded
    assert diagnostic.redacted_request_id.startswith("sha256:")
    assert len(diagnostic.redacted_request_id) == 23
    assert diagnostic.response_bytes == len("秘密".encode())
    assert diagnostic.summary == SAFE_SUMMARIES["authentication"]
    assert str(CopilotError(diagnostic)) == diagnostic.summary + " exit 7"


def test_schema_feedback_omits_response_values_and_unknown_field_names():
    try:
        AgentDecision.model_validate(
            {
                "action": "plan",
                "text": "private-response",
                "private-secret-field": "private-value",
            }
        )
    except ValidationError as error:
        feedback = schema_feedback(error, AgentDecision)
    encoded = json.dumps(feedback)
    assert "private" not in encoded
    assert feedback["errors"]
    assert all(set(item) == {"location", "code", "message"} for item in feedback["errors"])


def test_invalid_response_error_rejects_unrestricted_correction_feedback():
    diagnostic = make_diagnostics(
        "invalid_response",
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=0.1,
    )
    with pytest.raises(ValueError, match="schema_feedback"):
        CopilotInvalidResponseError(
            diagnostic,
            correction_feedback={
                "errors": [],
                "provider_payload": "private-provider-response",
            },
        )


def test_invalid_response_error_owns_immutable_safe_feedback():
    with pytest.raises(ValidationError) as caught:
        AgentDecision.model_validate(
            {
                "action": "plan",
                "text": "private-response",
                "private-secret-field": "private-value",
            }
        )
    source = schema_feedback(caught.value, AgentDecision)
    diagnostic = make_diagnostics(
        "invalid_response",
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=0.1,
    )
    error = CopilotInvalidResponseError(diagnostic, correction_feedback=source)
    expected = error.correction_feedback

    source["provider_payload"] = "private-provider-response"
    source_errors = source["errors"]
    assert isinstance(source_errors, list)
    source_errors.clear()
    returned = error.correction_feedback
    assert returned is not None
    returned["provider_payload"] = "private-provider-response"

    assert error.correction_feedback == expected
    assert "private" not in json.dumps(error.correction_feedback)


def test_schema_feedback_rejects_issue_snapshot_reassignment():
    with pytest.raises(ValidationError) as caught:
        AgentDecision.model_validate(
            {
                "action": "plan",
                "text": "private-response",
                "private-secret-field": "private-value",
            }
        )
    source = schema_feedback(caught.value, AgentDecision)
    diagnostic = make_diagnostics(
        "invalid_response",
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=0.1,
    )
    expected = CopilotInvalidResponseError(
        diagnostic, correction_feedback=source
    ).correction_feedback

    with pytest.raises(AttributeError, match="immutable"):
        setattr(source, "issues", ((("private_secret_location",), "private_secret_code"),))

    error = CopilotInvalidResponseError(diagnostic, correction_feedback=source)
    assert error.correction_feedback == expected
    assert "private" not in json.dumps(error.correction_feedback)


def test_schema_feedback_has_no_dict_snapshot_bypass():
    with pytest.raises(ValidationError) as caught:
        AgentDecision.model_validate(
            {
                "action": "plan",
                "text": "private-response",
                "private-secret-field": "private-value",
            }
        )
    source = schema_feedback(caught.value, AgentDecision)
    diagnostic = make_diagnostics(
        "invalid_response",
        stdout="",
        stderr="",
        exit_code=0,
        elapsed_seconds=0.1,
    )

    with pytest.raises(AttributeError):
        source.__dict__["_issues"] = ((("private_secret_location",), "private_secret_code"),)

    error = CopilotInvalidResponseError(diagnostic, correction_feedback=source)
    assert "private" not in json.dumps(error.correction_feedback)


def test_jsonl_request_identity_is_hashed_without_persisting_the_event():
    output = json.dumps(
        {
            "type": "error",
            "data": {"requestId": "private-provider-request", "message": "private-response"},
        }
    )
    diagnostic = make_diagnostics(
        "transport",
        stdout=output,
        stderr="",
        exit_code=7,
        elapsed_seconds=0.1,
    )
    assert diagnostic.redacted_request_id.startswith("sha256:")
    assert "private" not in diagnostic.model_dump_json()
    assert diagnostic.response_bytes == len(output.encode("utf-8"))
