import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from flight_agent.matlab import MatlabError
from flight_agent.verification.workbench_matlab import execute_workbench
from flight_agent.workbench_contracts import (
    MAX_WORKBENCH_RESULT_BYTES,
    WorkbenchRunRequest,
)

ROOT = Path(__file__).resolve().parents[1]


def workbench_request(kind: str = "script", timeout_seconds: int = 15) -> WorkbenchRunRequest:
    source = (
        "result = struct('value', params.value * 2);"
        if kind == "script"
        else "function result = analysis(params)\n"
        "result = struct('value', params.value * 2);\nend\n"
    )
    return WorkbenchRunRequest(
        id="3" * 32,
        filename="analysis.m",
        source=source,
        kind=kind,
        parameters={"value": 3},
        trusted=True,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (
            "web_script.m",
            """frequency_hz = params.frequency_hz;
amplitude = params.amplitude;
samples = params.samples;
t = linspace(0, 2, samples);
y = amplitude * sin(2 * pi * frequency_hz * t);
plot(t, y);
xlabel('Time (s)');
ylabel('Amplitude');
grid on;
result = struct('samples', samples, 'peak', max(y), 'minimum', min(y));
""",
        ),
        (
            "web_function.m",
            """function result = web_function(params)
t = linspace(0, 2, params.samples);
y = params.amplitude * cos(2 * pi * params.frequency_hz * t);
plot(t, y);
xlabel('Time (s)');
ylabel('Amplitude');
grid on;
result = struct('samples', params.samples, 'peak', max(y), 'minimum', min(y));
end
""",
        ),
    ],
)
def test_approved_workbench_examples_are_exact(name, content):
    assert (ROOT / "examples" / name).read_text(encoding="utf-8") == content


def live_workbench_request(kind: str) -> WorkbenchRunRequest:
    source = (
        "plot(1:3);\nresult = struct('value', params.value * 2);\n"
        if kind == "script"
        else "function result = analysis(params)\n"
        "plot(1:3);\n"
        "result = struct('value', params.value * 2);\n"
        "end\n"
    )
    return WorkbenchRunRequest(
        id=("4" if kind == "script" else "5") * 32,
        filename="analysis.m",
        source=source,
        kind=kind,
        parameters={"value": 3},
        trusted=True,
        timeout_seconds=120,
    )


@pytest.mark.matlab
@pytest.mark.skipif(
    os.environ.get("RUN_MATLAB_TESTS") != "1",
    reason="Live MATLAB is opt-in",
)
@pytest.mark.parametrize("kind", ["script", "function"])
def test_live_workbench_executes_result_and_figure(tmp_path, kind):
    request = live_workbench_request(kind)
    receipt, artifacts, log = execute_workbench(tmp_path / kind, request)
    assert receipt.result == {"value": 6}
    assert receipt.matlab_version
    assert [item.path for item in artifacts] == ["figure-1.png"]
    assert (tmp_path / kind / "figure-1.png").read_bytes().startswith(b"\x89PNG")


@pytest.mark.matlab
@pytest.mark.skipif(
    os.environ.get("RUN_MATLAB_TESTS") != "1",
    reason="Live MATLAB is opt-in",
)
def test_live_workbench_preserves_original_matlab_error(tmp_path):
    request = WorkbenchRunRequest(
        id="6" * 32,
        filename="analysis.m",
        source="error('flight_agent:acceptance', 'original live workbench failure');\n",
        kind="script",
        parameters={},
        trusted=True,
        timeout_seconds=120,
    )
    with pytest.raises(MatlabError, match="original live workbench failure"):
        execute_workbench(tmp_path / "failure", request)


@pytest.mark.parametrize("kind", ["script", "function"])
def test_workbench_executes_script_and_function_with_result_and_figure(
    tmp_path, fake_engine, monkeypatch, kind
):
    for name in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "LANGSMITH_API_KEY",
        "MATLAB_WORKER_TOKEN",
    ):
        monkeypatch.setenv(name, "do-not-forward")
    directory = tmp_path / "run"
    receipt, artifacts, log = execute_workbench(directory, workbench_request(kind))
    assert receipt.result == {"value": 6}
    assert receipt.matlab_version == "test-engine"
    assert [item.path for item in artifacts] == ["figure-1.png"]
    assert artifacts[0].media_type == "image/png"
    assert artifacts[0].role == "figure"
    assert artifacts[0].size == (directory / "figure-1.png").stat().st_size
    assert len(artifacts[0].sha256) == 64
    assert (directory / "figure-1.png").read_bytes().startswith(b"\x89PNG")
    assert "workbench log" in log
    assert (directory / "code" / "analysis.m").read_text() == workbench_request(kind).source


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [
        ("startup_error", "license"),
        ("execution_error", "original MATLAB solver failure"),
        ("workbench_missing_receipt", "receipt"),
        ("workbench_wrong_digest", "digest"),
        ("workbench_missing_result", "result"),
        ("workbench_nan", "JSON"),
        ("workbench_inf", "JSON"),
        ("workbench_oversized_result", "limit"),
        ("workbench_log_flood", "log"),
        ("workbench_too_many_figures", "figure"),
        ("workbench_invalid_png", "PNG"),
        ("workbench_large_dimensions", "dimensions"),
        ("workbench_large_png", "2 MB"),
        ("workbench_receipt_symlink", "receipt"),
        ("workbench_artifact_symlink", "symlink"),
    ],
)
def test_workbench_failures_are_explicit(
    tmp_path, fake_engine, monkeypatch, mode, fragment
):
    monkeypatch.setenv("TEST_ENGINE_MODE", mode)
    with pytest.raises(MatlabError, match=fragment):
        execute_workbench(tmp_path / "run", workbench_request())


def test_workbench_timeout_stops_owned_process_group(
    tmp_path, fake_engine, monkeypatch
):
    monkeypatch.setenv("TEST_ENGINE_MODE", "workbench_block")
    with pytest.raises(MatlabError, match="timed out"):
        execute_workbench(tmp_path / "run", workbench_request(timeout_seconds=1))


def test_workbench_accepts_json_null_result(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("TEST_ENGINE_MODE", "workbench_null")
    receipt, _, _ = execute_workbench(tmp_path / "run", workbench_request())
    assert receipt.result is None


def test_workbench_rejects_complete_receipt_over_exact_four_mb(
    tmp_path, fake_engine, monkeypatch
):
    directory = tmp_path / "run"
    monkeypatch.setenv("TEST_ENGINE_MODE", "workbench_oversized_receipt")

    with pytest.raises(MatlabError, match="receipt exceeds"):
        execute_workbench(directory, workbench_request())

    receipt = directory / "receipt.json"
    assert receipt.stat().st_size > MAX_WORKBENCH_RESULT_BYTES
    result = json.loads(receipt.read_text())["result"]
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= MAX_WORKBENCH_RESULT_BYTES


def test_workbench_cleanup_uncertainty_never_becomes_success(
    tmp_path, fake_engine, monkeypatch
):
    import flight_agent.verification.workbench_matlab as module

    monkeypatch.setattr(
        module,
        "_stop_worker",
        lambda process, label: SimpleNamespace(error="permission denied"),
    )
    with pytest.raises(MatlabError, match="cleanup uncertain.*permission denied"):
        execute_workbench(tmp_path / "run", workbench_request())


def test_workbench_uses_fixed_wrapper_without_evaluating_uploaded_filename():
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "flight_agent"
        / "matlab_support"
        / "flight_agent_execute_workbench.m"
    ).read_text(encoding="utf-8")
    assert "flight_agent_execute_workbench(" in wrapper
    assert "feval(entry, params)" in wrapper
    assert re.search(r"\beval\(", wrapper) is None
    assert "run(source_path)" in wrapper
