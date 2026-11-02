import pytest
from pydantic import ValidationError

from flight_agent.workbench_contracts import (
    WorkbenchArtifact,
    WorkbenchExecutionReceipt,
    WorkbenchRunRecord,
    WorkbenchRunRequest,
    workbench_request_digest,
)


def request_data():
    return {
        "id": "a" * 32,
        "filename": "calculation.m",
        "source": "result = struct('value', params.value * 2);",
        "kind": "script",
        "parameters": {"value": 3},
        "trusted": True,
        "timeout_seconds": 120,
    }


def record_data(**changes):
    data = {
        "id": "a" * 32,
        "request_sha256": "b" * 64,
        "status": "queued",
        "stage": "queued",
        "created_at": "2026-11-02T11:40:32+08:00",
        "updated_at": "2026-11-02T11:40:32+08:00",
        "filename": "calculation.m",
        "kind": "script",
        "matlab_version": None,
        "result": None,
        "error": None,
        "log": "",
        "artifacts": [],
    }
    data.update(changes)
    return data


def test_workbench_request_preserves_legacy_script_contract():
    request = WorkbenchRunRequest.model_validate(request_data())
    assert request.filename == "calculation.m"
    assert request.parameters == {"value": 3}
    assert len(workbench_request_digest(request)) == 64


def test_workbench_function_requires_matching_single_params_signature():
    data = request_data()
    data.update(
        kind="function",
        source="function result = calculation(params)\nresult = params;\nend",
    )
    assert WorkbenchRunRequest.model_validate(data).kind == "function"
    data["source"] = "function result = wrong(params)\nresult = params;\nend"
    with pytest.raises(ValidationError, match="function"):
        WorkbenchRunRequest.model_validate(data)


@pytest.mark.parametrize(
    "keyword",
    [
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
    ],
)
def test_workbench_request_rejects_matlab_keyword_filename(keyword):
    data = request_data()
    data["filename"] = f"{keyword}.m"
    with pytest.raises(ValidationError, match="keyword"):
        WorkbenchRunRequest.model_validate(data)


def test_workbench_function_ignores_declarations_inside_block_comments():
    data = request_data()
    data.update(
        kind="function",
        source=(
            "%{\n"
            "function result = calculation(params)\n"
            "result = params;\n"
            "end\n"
            "%}\n"
        ),
    )
    with pytest.raises(ValidationError, match="function"):
        WorkbenchRunRequest.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("filename", "../calculation.m"),
        ("filename", "for.m"),
        ("filename", "flight_agent_execute_workbench.m"),
        ("source", ""),
        ("source", "\0"),
        ("source", "x" * 1_000_001),
        ("parameters", {"value": float("nan")}),
        ("parameters", {"value": "x" * 100_001}),
        ("trusted", False),
        ("trusted", "true"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 601),
    ],
)
def test_workbench_request_rejects_untrusted_or_unbounded_input(field, value):
    data = request_data()
    data[field] = value
    with pytest.raises(ValidationError):
        WorkbenchRunRequest.model_validate(data)


def test_workbench_receipt_accepts_null_result():
    receipt = WorkbenchExecutionReceipt.model_validate(
        {
            "request_sha256": "b" * 64,
            "result": None,
            "matlab_version": "R2026a",
            "figures": [],
        }
    )
    assert receipt.result is None


def test_completed_record_requires_version_and_completed_stage():
    with pytest.raises(ValidationError):
        WorkbenchRunRecord.model_validate(record_data(status="completed"))
    with pytest.raises(ValidationError):
        WorkbenchRunRecord.model_validate(
            record_data(
                status="completed",
                stage="executing",
                matlab_version="R2026a",
            )
        )


def test_completed_record_accepts_null_result():
    record = WorkbenchRunRecord.model_validate(
        record_data(status="completed", stage="completed", matlab_version="R2026a")
    )
    assert record.result is None


def test_completed_stage_requires_completed_status():
    with pytest.raises(ValidationError):
        WorkbenchRunRecord.model_validate(record_data(status="running", stage="completed"))


def test_record_rejects_duplicate_artifact_paths():
    artifact = {
        "path": "figure.png",
        "sha256": "c" * 64,
        "size": 100,
        "media_type": "image/png",
        "role": "figure",
    }
    with pytest.raises(ValidationError, match="unique"):
        WorkbenchRunRecord.model_validate(
            record_data(
                status="completed",
                stage="completed",
                matlab_version="R2026a",
                artifacts=[artifact, artifact],
            )
        )


def test_record_rejects_non_png_figure_artifacts():
    with pytest.raises(ValidationError):
        WorkbenchArtifact.model_validate(
            {
                "path": "figure.jpg",
                "sha256": "c" * 64,
                "size": 100,
                "media_type": "image/png",
                "role": "figure",
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"result": {"value": 1}, "matlab_version": "R2026a"},
        {"result": {"value": 1}},
        {"artifacts": [{
            "path": "figure.png",
            "sha256": "c" * 64,
            "size": 100,
            "media_type": "image/png",
            "role": "figure",
        }]},
    ],
)
def test_failed_record_rejects_success_output(changes):
    with pytest.raises(ValidationError):
        WorkbenchRunRecord.model_validate(
            record_data(status="failed", stage="failed", error="execution failed", **changes)
        )


def test_record_rejects_oversized_log():
    with pytest.raises(ValidationError):
        WorkbenchRunRecord.model_validate(record_data(log="x" * 1_000_001))


@pytest.mark.parametrize(
    "result",
    [
        {"value": float("inf")},
        "x" * 4_000_001,
    ],
)
def test_record_rejects_nonfinite_or_oversized_result(result):
    with pytest.raises(ValidationError):
        WorkbenchRunRecord.model_validate(
            record_data(
                status="completed",
                stage="completed",
                matlab_version="R2026a",
                result=result,
            )
        )
