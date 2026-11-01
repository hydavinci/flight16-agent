import hashlib
import json
import re

from pydantic import BaseModel, ValidationError

from .runtime.planning_contracts import (
    SAFE_SUMMARIES,
    CopilotDiagnostics,
    CopilotFailureCategory,
)

DIAGNOSTIC_SCAN_BYTES = 8192
_SCHEMA_FEEDBACK_MESSAGE = "Structured response schema validation failed."
_SchemaIssue = tuple[tuple[str, ...], str]


def _bounded_text(value: str) -> str:
    return value.encode("utf-8")[:DIAGNOSTIC_SCAN_BYTES].decode("utf-8", errors="ignore")


def classify_failure(stdout: str, stderr: str) -> CopilotFailureCategory:
    text = _bounded_text(stderr) + "\n" + _bounded_text(stdout)
    patterns: tuple[tuple[CopilotFailureCategory, str], ...] = (
        (
            "transport",
            r"\b407\b|websocket|proxy|dns|ENOTFOUND|EAI_AGAIN|ECONNRESET|"
            r"ECONNREFUSED|connection (?:failed|closed|reset)|network unreachable",
        ),
        (
            "authentication",
            r"\b401\b|not logged in|not authenticated|login required|"
            r"authentication (?:failed|required)|token (?:has )?expired|"
            r"expired (?:token|credentials)|please (?:log|sign) in",
        ),
        ("authorization", r"\b403\b|model access denied|account access denied|forbidden"),
    )
    for category, expression in patterns:
        if re.search(expression, text, re.IGNORECASE):
            return category
    return "process_failure"


def make_diagnostics(
    category: CopilotFailureCategory,
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed_seconds: float,
) -> CopilotDiagnostics:
    request_id = None
    match = re.search(
        r"(?im)^\s*(?:x-github-request-id|request-id)\s*:\s*"
        r"([a-z0-9][a-z0-9_.:-]{0,127})\s*$",
        _bounded_text(stderr),
    )
    if match is not None:
        request_id = "sha256:" + hashlib.sha256(match[1].encode("utf-8")).hexdigest()[:16]
    if request_id is None:
        for line in _bounded_text(stdout).splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            containers = [event, data] if isinstance(data, dict) else [event]
            for container in containers:
                value = container.get("requestId", container.get("request_id"))
                if isinstance(value, str) and re.fullmatch(
                    r"[a-z0-9][a-z0-9_.:-]{0,127}",
                    value,
                    re.IGNORECASE,
                ):
                    request_id = "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                    break
            if request_id is not None:
                break
    return CopilotDiagnostics(
        category=category,
        redacted_request_id=request_id,
        exit_code=exit_code,
        elapsed_seconds=max(0.0, elapsed_seconds),
        response_bytes=len(stdout.encode("utf-8")),
        summary=SAFE_SUMMARIES[category],
    )


class CopilotError(ValueError):
    def __init__(self, diagnostics: CopilotDiagnostics):
        self.diagnostics = diagnostics
        suffix = "" if diagnostics.exit_code is None else f" exit {diagnostics.exit_code}"
        super().__init__(diagnostics.summary + suffix)


class CopilotInvalidResponseError(CopilotError):
    def __init__(
        self,
        diagnostics: CopilotDiagnostics,
        *,
        correction_feedback: dict[str, object] | None = None,
    ):
        self._correction_issues = _safe_feedback_issues(correction_feedback)
        super().__init__(diagnostics)

    @property
    def correction_feedback(self) -> dict[str, object] | None:
        if self._correction_issues is None:
            return None
        return _feedback_dict(self._correction_issues)


class CopilotTimeoutError(CopilotError):
    def __init__(
        self,
        timeout_seconds: float,
        *,
        diagnostics: CopilotDiagnostics | None = None,
        round_number: int | None = None,
        total_budget_seconds: float | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        super().__init__(
            diagnostics
            or make_diagnostics(
                "timeout",
                stdout="",
                stderr="",
                exit_code=None,
                elapsed_seconds=timeout_seconds,
            )
        )
        if round_number is None or total_budget_seconds is None:
            self.args = (f"Copilot CLI timed out after {timeout_seconds:g}s; not retried",)
        else:
            self.args = (
                f"Copilot CLI 规划第 {round_number} 轮超时："
                f"本轮可用 {timeout_seconds:g}s，原总预算 {total_budget_seconds:g}s"
                "（含前轮耗时，两轮共用）。未自动重试；"
                "本次未生成新方案或触发执行。",
            )


def _feedback_dict(issues: tuple[_SchemaIssue, ...]) -> dict[str, object]:
    return {
        "errors": [
            {
                "location": list(location),
                "code": code,
                "message": _SCHEMA_FEEDBACK_MESSAGE,
            }
            for location, code in issues
        ]
    }


class _SchemaFeedback(dict[str, object]):
    __slots__ = ("_issues",)
    _issues: tuple[_SchemaIssue, ...]

    def __init__(self, issues: tuple[_SchemaIssue, ...]):
        object.__setattr__(self, "_issues", issues)
        super().__init__(_feedback_dict(issues))

    @property
    def issues(self) -> tuple[_SchemaIssue, ...]:
        return self._issues

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("schema feedback attributes are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("schema feedback attributes are immutable")


def _safe_feedback_issues(
    feedback: dict[str, object] | None,
) -> tuple[_SchemaIssue, ...] | None:
    if feedback is None:
        return None
    if not isinstance(feedback, _SchemaFeedback):
        raise ValueError("correction_feedback must be created by schema_feedback")
    if len(feedback.issues) > 8:
        raise ValueError("schema_feedback exceeds the error limit")
    safe_issues: list[_SchemaIssue] = []
    for location, code in feedback.issues:
        if len(location) > 10 or re.fullmatch(r"[a-z0-9_]{1,100}", code) is None:
            raise ValueError("schema_feedback contains an invalid issue")
        if any(
            re.fullmatch(r"(?:\[field\]|[A-Za-z_][A-Za-z0-9_]{0,99}|[0-9]{1,10})", part) is None
            for part in location
        ):
            raise ValueError("schema_feedback contains an invalid location")
        safe_issues.append((tuple(location), code))
    return tuple(safe_issues)


def schema_feedback(error: ValidationError, schema: type[BaseModel]) -> dict[str, object]:
    field_names: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                field_names.update(str(key) for key in properties)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema.model_json_schema())
    issues: list[_SchemaIssue] = []
    for issue in error.errors(
        include_input=False,
        include_context=False,
        include_url=False,
    )[:8]:
        code = str(issue["type"])
        if re.fullmatch(r"[a-z0-9_]{1,100}", code) is None:
            code = "schema_validation"
        issues.append(
            (
                tuple(
                    str(part) if isinstance(part, int) or part in field_names else "[field]"
                    for part in issue["loc"][:10]
                ),
                code,
            )
        )
    return _SchemaFeedback(tuple(issues))
