import json
import math
import os
from pathlib import Path

import pytest

from tests.verification_helpers import project_tree, verification_module


def run_request(root, kinds=("step_response",), timeout=15):
    contracts = verification_module("contracts")
    project = verification_module("projects").ProjectInspector().inspect(str(root))
    return contracts.AnalysisRunRequest(
        id="3" * 32,
        project_root=str(root),
        project_fingerprint=project.fingerprint,
        analyses=list(kinds),
        trim_point=1,
        trusted=True,
        timeout_seconds=timeout,
    )


def test_isolated_analysis_subprocess_preserves_source_and_cleans_engine(
    tmp_path, fake_engine, monkeypatch
):
    adapter = verification_module("matlab")
    root = project_tree(tmp_path / "source")
    request = run_request(root)
    marker = tmp_path / "quit.txt"
    monkeypatch.setenv("TEST_QUIT_FILE", str(marker))
    monkeypatch.setenv("LLM_API_KEY", "must-not-reach-matlab")
    result = adapter.execute_project(tmp_path / "job", request)
    assert result.project_fingerprint == request.project_fingerprint
    assert result.analysis("step_response").metrics["overshoot"].value == 5.0
    assert result.artifacts and all(item.sha256 for item in result.artifacts)
    assert marker.read_text() == "quit"
    assert (tmp_path / "job" / "project" / "initialize.m").exists()
    assert not (root / "analysis.json").exists()
    assert verification_module("projects").ProjectInspector().inspect(str(root)).fingerprint == (
        request.project_fingerprint
    )


@pytest.mark.parametrize(
    "mode,fragment",
    [
        ("execution_error", "solver"),
        ("verification_wrong_context", "context"),
        ("verification_missing_artifact", "artifact"),
        ("verification_bad_image", "PNG"),
        ("verification_missing_branch", "analyses"),
    ],
)
def test_analysis_output_failures_are_explicit(tmp_path, fake_engine, monkeypatch, mode, fragment):
    from flight_agent.matlab import MatlabError

    adapter = verification_module("matlab")
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("TEST_ENGINE_MODE", mode)
    with pytest.raises(MatlabError, match=fragment):
        adapter.execute_project(tmp_path / "job", run_request(root))


def test_analysis_requires_trusted_project_consent(tmp_path):
    contracts = verification_module("contracts")
    root = project_tree(tmp_path / "source")
    data = run_request(root).model_dump()
    data["trusted"] = False
    with pytest.raises(ValueError, match="trusted"):
        contracts.AnalysisRunRequest.model_validate(data)


def test_analysis_rejects_source_changes_before_execution(tmp_path, fake_engine):
    adapter = verification_module("matlab")
    root = project_tree(tmp_path / "source")
    request = run_request(root)
    (root / "data" / "input.mat").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed|fingerprint"):
        adapter.execute_project(tmp_path / "job", request)


def test_analysis_timeout_is_a_failure_not_empty_success(tmp_path, fake_engine, monkeypatch):
    from flight_agent.matlab import MatlabError

    adapter = verification_module("matlab")
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("TEST_ENGINE_MODE", "verification_block")
    with pytest.raises(MatlabError, match="timed out"):
        adapter.execute_project(tmp_path / "job", run_request(root, timeout=1))


def test_completed_run_directory_cannot_be_executed_again(tmp_path, fake_engine):
    adapter = verification_module("matlab")
    root = project_tree(tmp_path / "source")
    request = run_request(root)
    adapter.execute_project(tmp_path / "job", request)
    with pytest.raises(ValueError, match="already|exists"):
        adapter.execute_project(tmp_path / "job", request)


@pytest.fixture(scope="module")
def real_engine():
    if os.environ.get("RUN_MATLAB_TESTS") != "1":
        pytest.skip("Live MATLAB is opt-in")
    import matlab.engine

    engine = matlab.engine.start_matlab("-nodesktop -nosplash")
    engine.addpath(
        str(Path(__file__).resolve().parents[1] / "src/flight_agent/matlab_support"), nargout=0
    )
    try:
        yield engine
    finally:
        engine.quit()


def step_metrics(engine, t, u, y, response_times=None):
    import matlab

    for name, values in (("fa_t", t), ("fa_u", u), ("fa_y", y), ("fa_ty", response_times or t)):
        engine.workspace[name] = matlab.double([[value] for value in values])
    return json.loads(
        engine.eval("jsonencode(flight_agent_step_metrics(fa_t,fa_u,fa_ty,fa_y,struct))")
    )


@pytest.mark.matlab
def test_real_step_metrics_account_for_delay_and_nonzero_initial_value(real_engine):
    times = [index / 100 for index in range(1501)]
    inputs = [1.0 if time < 2 else 3.0 for time in times]
    responses = [2.0 if time < 2 else 2 + 4 * (1 - math.exp(-(time - 2))) for time in times]
    result = step_metrics(real_engine, times, inputs, responses)
    assert result["valid"]
    assert result["metrics"]["overshoot"]["value"] == pytest.approx(0.0, abs=0.01)
    assert result["metrics"]["settling_time"]["value"] == pytest.approx(2.9957, abs=0.03)
    assert result["details"]["step_time"] == 2.0
    assert result["details"]["initial_value"] == 2.0
    assert result["details"]["final_value"] == pytest.approx(6.0, abs=0.001)
    assert result["details"]["settling_band"] == 0.05


@pytest.mark.matlab
@pytest.mark.parametrize("kind", ["constant", "ramp", "oscillation", "short", "multiple_steps"])
def test_real_invalid_step_evidence_is_not_forced_into_metrics(real_engine, kind):
    times = [index / 100 for index in range(1501)]
    inputs = [0.0 if time < 2 else 1.0 for time in times]
    responses = [0.0 if time < 2 else 1 - math.exp(-(time - 2)) for time in times]
    if kind == "constant":
        responses = [3.0 for _ in times]
    elif kind == "ramp":
        responses = [max(0.0, time - 2) for time in times]
    elif kind == "oscillation":
        responses = [0.0 if time < 2 else 1 + math.sin(time * 2) for time in times]
    elif kind == "short":
        times, inputs, responses = times[:250], inputs[:250], responses[:250]
    else:
        inputs = [value if time < 10 else 2.0 for time, value in zip(times, inputs, strict=True)]
    result = step_metrics(real_engine, times, inputs, responses)
    assert not result["valid"]
    assert result["diagnostics"]
    assert not result["metrics"]


@pytest.mark.matlab
def test_real_margin_metrics_keep_infinite_gain_and_finite_phase(real_engine):
    result = json.loads(real_engine.eval("jsonencode(flight_agent_margin_metrics(tf(4,[1 2 1])))"))
    assert result["valid"]
    assert result["metrics"]["phase_margin"]["value"] == pytest.approx(60.0, abs=0.01)
    assert result["metrics"]["gain_margin"]["special"] == "positive_infinity"
    assert result["details"]["phase_crossings"][0]["frequency"] == pytest.approx(
        math.sqrt(3), abs=0.001
    )


@pytest.mark.matlab
def test_real_margin_metrics_convert_gain_multiplier_to_db(real_engine):
    result = json.loads(
        real_engine.eval("jsonencode(flight_agent_margin_metrics(tf(1,[1 3 2 0])))")
    )
    assert result["valid"]
    assert result["metrics"]["gain_margin"]["value"] == pytest.approx(15.563025, abs=0.01)


@pytest.mark.matlab
def test_real_simulink_project_executes_both_branches(tmp_path, real_engine):
    adapter = verification_module("matlab")
    fixture = Path(__file__).parent / "fixtures" / "verification"
    real_engine.addpath(str(fixture), nargout=0)
    source = tmp_path / "synthetic source"
    real_engine.feval("create_verification_fixture", str(source), nargout=0)
    request = run_request(source, ("step_response", "stability_margins"), timeout=180)
    result = adapter.execute_project(tmp_path / "actual run", request)
    time_result = result.analysis("step_response")
    frequency_result = result.analysis("stability_margins")
    assert time_result.valid, time_result.diagnostics
    assert frequency_result.valid, frequency_result.diagnostics
    assert time_result.metrics["overshoot"].value == pytest.approx(20.78796, abs=0.1)
    assert frequency_result.metrics["phase_margin"].value == pytest.approx(60.0, abs=0.1)
    assert {item.path for item in result.artifacts} == {
        "step-response.mat",
        "step-response.png",
        "stability-margins.mat",
        "bode.png",
    }
