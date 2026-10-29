"""Opt-in actual Engine + HTTP/runtime acceptance; only the planner is controlled.

All model/code generation below is test fixture preparation. Production runs
existing, fingerprinted entries/models only. No actual user project is claimed.
"""

import json
import math
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flight_agent.verification.local_matlab import LocalMatlabBackend
from flight_agent.verification.matlab_projects import discover_matlab_project
from tests.test_matlab_project_acceptance import (
    PreparedCase,
    acceptance_app,
    confirm_http,
    download_results,
    prepare_offline_case,
    propose_http,
)

pytestmark = pytest.mark.matlab


@pytest.fixture
def actual_backend(monkeypatch):
    if os.environ.get("RUN_MATLAB_TESTS") != "1":
        pytest.skip("Actual MATLAB/Simulink is opt-in; no fake fallback")
    assert os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") == "1"
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    return LocalMatlabBackend.from_environment()


@pytest.fixture
def thermal_project(tmp_path):
    case = prepare_offline_case(tmp_path / "thermal-bench", "entry")
    (case.root / case.selected).write_text(
        "function result = thermal_response()\n"
        "% Provider's prepared MATLAB-only analytic thermal bench fixture.\n"
        "t = (0:0.002:16)'; elapsed = max(t-1, 0);\n"
        "zeta = 0.6; omega = 2; wd = omega*sqrt(1-zeta^2);\n"
        "y = 1-exp(-zeta*omega*elapsed).*(cos(wd*elapsed) + "
        "zeta/sqrt(1-zeta^2)*sin(wd*elapsed));\n"
        "u = double(t>=1);\n"
        "result.signals.excitation = struct('time',t,'values',u,"
        "'time_unit','s','unit','V');\n"
        "result.signals.temperature = struct('time',t,'values',y,"
        "'time_unit','s','unit','K');\nend\n"
    )
    return case


@pytest.fixture(params=["script", "base_function", "mixed"])
def nonlinear_project(tmp_path, actual_backend, request):
    import matlab.engine

    case = PreparedCase(
        tmp_path / "actuator-lab", "simulink", "models/limited_loop.slx",
        "setup/prepare.m", "logsout", "lever", "stroke", "1", "m", "limited",
    )
    (case.root / "models").mkdir(parents=True)
    (case.root / "setup").mkdir()
    initialization = (
        "feedback_gain = 2; actuator_limit = 0.5; decay_rate = 1;\n"
        "reference_level = 1; excitation_time = 1;\n"
        "horizon = 16; integration_step = 0.002;\n"
        "condition = 'limited';\n"
    )
    if request.param == "base_function":
        initialization = (
            "function prepare()\n" + initialization
            + "for name = {'feedback_gain','actuator_limit','decay_rate',"
            "'reference_level','excitation_time','horizon','integration_step','condition'}\n"
            "assignin('base',name{1},eval(name{1}));\nend\nend\n"
        )
    elif request.param == "mixed":
        initialization += (
            "assignin('base','actuator_limit',9);\n"
            "assignin('base','decay_rate',decay_rate);\n"
            "assignin('base','horizon',horizon);\n"
            "assignin('base','integration_step',integration_step);\n"
            "clear decay_rate horizon integration_step;\n"
        )
    (case.root / case.initializer).write_text(initialization)
    case.document()
    # Fixture-owned MATLAB prepares an actual nonlinear feedback model, then exits.
    engine = matlab.engine.start_matlab()
    try:
        engine.workspace["fixture_file"] = str(case.root / case.selected)
        engine.eval(
            "m='limited_loop'; new_system(m); "
            "add_block('simulink/Sources/Step',[m '/Stick'],"
            "'Time','excitation_time','Before','0','After','reference_level'); "
            "add_block('simulink/Math Operations/Sum',[m '/Error'],'Inputs','+-'); "
            "add_block('simulink/Math Operations/Gain',[m '/Feedback'],"
            "'Gain','feedback_gain'); "
            "add_block('simulink/Discontinuities/Saturation',[m '/Limiter'],"
            "'UpperLimit','actuator_limit','LowerLimit','-actuator_limit'); "
            "add_block('simulink/Math Operations/Sum',[m '/Dynamics'],'Inputs','+-'); "
            "add_block('simulink/Math Operations/Gain',[m '/Decay'],'Gain','decay_rate'); "
            "add_block('simulink/Continuous/Integrator',[m '/State'],'InitialCondition','0'); "
            "add_line(m,'Stick/1','Error/1'); add_line(m,'State/1','Error/2'); "
            "add_line(m,'Error/1','Feedback/1'); add_line(m,'Feedback/1','Limiter/1'); "
            "add_line(m,'Limiter/1','Dynamics/1'); add_line(m,'State/1','Decay/1'); "
            "add_line(m,'Decay/1','Dynamics/2'); add_line(m,'Dynamics/1','State/1'); "
            "p=get_param([m '/Stick'],'PortHandles'); "
            "set_param(p.Outport(1),'DataLogging','on','DataLoggingNameMode','Custom',"
            "'DataLoggingName','lever'); "
            "p=get_param([m '/State'],'PortHandles'); "
            "set_param(p.Outport(1),'DataLogging','on','DataLoggingNameMode','Custom',"
            "'DataLoggingName','stroke'); "
            "set_param(m,'SolverType','Fixed-step','Solver','ode4',"
            "'FixedStep','integration_step','StopTime','horizon','SignalLogging','on',"
            "'SignalLoggingName','logsout','ReturnWorkspaceOutputs','on'); "
            "save_system(m,fixture_file); close_system(m,0);",
            nargout=0,
        )
    finally:
        engine.quit()
    return case


def independent_thermal_settling(band):
    """Last true-final-value crossing, independent scalar bisection, not stepinfo."""
    def error(t):
        return -math.exp(-1.2 * t) * (math.cos(1.6 * t) + 0.75 * math.sin(1.6 * t))

    # Bracket all crossings on a fine reference grid, then solve the LAST crossing.
    outside = [i / 1000 for i in range(15001) if abs(error(i / 1000)) > band]
    low = outside[-1]
    high = low + 0.001
    for _ in range(45):
        middle = (low + high) / 2
        if abs(error(middle)) > band:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def run_actual_pipeline(tmp_path, monkeypatch, backend, case):
    before = discover_matlab_project(str(case.root))
    app, runtime, contexts = acceptance_app(tmp_path / "web", monkeypatch, case, backend)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, case)
        assert not list((tmp_path / "web").rglob("local-matlab"))
        done = confirm_http(client, headers, pending, timeout=240)
        assert done["status"] == "completed", done["error"]
        payload, word = download_results(client, headers, done)
    record = runtime.store.get(done["id"])
    assert discover_matlab_project(str(case.root)) == before
    assert record.task.budget_state.calls == record.task.budget_state.rounds == 4
    assert record.task.result.unmet_requirements == []
    assert payload["evaluation_requested"]
    details = payload["evidence"]["details"]["dependencies"]
    directory = Path(record.task.result_directory)
    for node in ("execute", "measure"):
        metadata = next(
            item for item in record.task.artifacts
            if item.node_id == node and Path(item.path).name == "metadata.json"
        )
        data = json.loads((directory / metadata.path).read_text())
        assert data["matlab_release"] != "test-only"
        assert data["matlab_version"] != "test-only-fake-engine"
        assert data["conditions"] == {"condition": case.condition}
        if node == "execute":
            assert data["inputs"]["input_signal"]["source"] == case.namespace
            selected = data["inputs"]["model"] if case.mode == "simulink" else (
                data["inputs"]["entry"]["path"]
            )
            assert selected == case.selected
            if case.initializer:
                assert data["inputs"]["initializer"]["path"] == case.initializer
                assert data["settings"]["FixedStep"] == "integration_step"
        print(f"Actual {node} MATLAB: {data['matlab_version']} ({data['matlab_release']})")
    assert details["measure"]["settling_band"] == case.band
    assert details["measure"]["input_signal"] == {
        "name": case.input_name, "unit": case.input_unit,
    }
    assert details["measure"]["response_signal"] == {
        "name": case.response_name, "unit": case.response_unit,
    }
    assert '"values":' not in json.dumps(contexts)
    text = "\n".join(paragraph.text for paragraph in word.paragraphs)
    for value in (
        before.fingerprint, "settling_band", "initial_value", "final_value",
        case.analysis, case.criteria, case.condition,
    ):
        assert value in text
    assert word.inline_shapes
    metrics = {item.metric_id: item.value for item in record.task.result.observations}
    print(
        f"\nACTUAL {case.mode} {case.selected}: metrics={metrics}, "
        f"band={case.band}, source={case.namespace}, condition={case.condition}, "
        f"initial={details['measure']['initial_value']}, "
        f"tail_final={details['measure']['final_value']}, "
        f"series={details['execute']['series']}"
    )
    return metrics, details, record, word


def test_actual_matlab_only_full_http_pipeline(
    tmp_path, monkeypatch, actual_backend, thermal_project,
):
    metrics, details, record, word = run_actual_pipeline(
        tmp_path, monkeypatch, actual_backend, thermal_project,
    )
    reference_overshoot = 100 * math.exp(-math.pi * 0.6 / 0.8)
    reference_settling = independent_thermal_settling(0.05)
    print(f"Independent thermal references: {reference_overshoot=}, {reference_settling=}")
    # 2 ms samples, finite 15 s tail: allow 0.01 percentage point and 5 ms.
    assert metrics["overshoot"] == pytest.approx(reference_overshoot, abs=0.01)
    assert metrics["settling_time"] == pytest.approx(reference_settling, abs=0.005)
    assert details["measure"]["initial_value"] == pytest.approx(0, abs=1e-12)
    assert details["measure"]["final_value"] == pytest.approx(1, abs=1e-6)
    assert record.task.result.evaluation.status == "pass"
    assert word.inline_shapes


@pytest.mark.parametrize("band,expected", [(0.05, "pass"), (0.02, "fail")])
def test_actual_nonlinear_closed_loop_full_http_pipeline(
    tmp_path, monkeypatch, actual_backend, nonlinear_project, band, expected,
):
    nonlinear_project.band = band
    nonlinear_project.document()
    metrics, details, record, word = run_actual_pipeline(
        tmp_path, monkeypatch, actual_backend, nonlinear_project,
    )
    reference = math.log(1 / band)
    print(f"Independent saturated-feedback reference: {reference=}, band={band}")
    assert metrics["overshoot"] == pytest.approx(0, abs=0.001)
    assert metrics["settling_time"] == pytest.approx(reference, abs=0.005)
    assert details["measure"]["initial_value"] == pytest.approx(0, abs=1e-12)
    assert details["measure"]["final_value"] == pytest.approx(0.5, abs=2e-6)
    assert record.task.result.evaluation.status == expected
    assert word.inline_shapes


@pytest.mark.parametrize("nonlinear_project", ["base_function"], indirect=True)
def test_actual_simulink_compile_failure_keeps_nested_causes(
    tmp_path, monkeypatch, actual_backend, nonlinear_project,
):
    initializer = nonlinear_project.root / nonlinear_project.initializer
    initializer.write_text(
        initializer.read_text().replace("'feedback_gain','actuator_limit',", "")
    )
    before = discover_matlab_project(str(nonlinear_project.root))
    app, runtime, _ = acceptance_app(
        tmp_path / "web", monkeypatch, nonlinear_project, actual_backend,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, nonlinear_project)
        done = confirm_http(client, headers, pending, timeout=240)
    assert done["status"] == "failed"
    assert "multiple causes" in done["error"]
    assert "feedback_gain" in done["error"] and "actuator_limit" in done["error"]
    assert len(done["error"]) <= 6000
    task = runtime.store.get(done["id"]).task
    assert [call.status for call in task.calls] == ["failed"]
    assert not task.result.observations and task.result.evaluation is None
    assert discover_matlab_project(str(nonlinear_project.root)) == before
    print("ACTUAL Simulink compile failure: nested causes retained, no successful metrics")


@pytest.mark.parametrize("damage", ["multistep", "nonsteady", "missing"])
def test_actual_bad_project_data_never_passes(
    tmp_path, monkeypatch, actual_backend, thermal_project, damage,
):
    path = thermal_project.root / thermal_project.selected
    text = path.read_text()
    if damage == "multistep":
        text = text.replace("u = double(t>=1);", "u = double(t>=1) + double(t>=8);")
    elif damage == "nonsteady":
        text = text.replace("u = double(t>=1);", "u = double(t>=1); y = elapsed;")
    else:
        text = text.replace("result.signals.temperature", "result.signals.not_selected")
    path.write_text(text)
    app, runtime, _ = acceptance_app(tmp_path / "web", monkeypatch, thermal_project, actual_backend)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, thermal_project)
        done = confirm_http(client, headers, pending, timeout=240)
        result = runtime.store.get(done["id"]).task.result
        if damage == "missing":
            assert done["status"] == "failed", done["error"]
            assert "missing" in done["error"].lower()
        else:
            assert done["status"] == "completed", done["error"]
            payload, _ = download_results(client, headers, done)
            assert result.evaluation.status == "undetermined"
            assert not result.observations
            assert payload["execution_status"] == "partial"
            assert payload["unmet_requirements"]
            assert damage == "multistep" or "steady" in json.dumps(payload).lower()
    print(f"ACTUAL invalid {damage}: status={done['status']}, no passing evaluation")


@pytest.mark.parametrize("script_preparation", [False, True])
def test_actual_linearize_preparation_workspace_visibility(
    tmp_path, actual_backend, script_preparation,
):
    """Parent-owned real check: base -> script, reserved names, and last initializer wins."""
    import matlab.engine

    from flight_agent.verification.matlab_contracts import LocalMatlabRequest

    root = tmp_path / "workspace-project"
    root.mkdir()
    (root / "choose_point.m").write_text(
        "function choose_point(value)\n"
        "assignin('base','point_index',value);\n"
        "assignin('base','fa_request',-999);\nend\n"
    )
    (root / "prepare_script.m").write_text(
        "prep_seen = point_index;\nassert(prep_seen == 7);\n"
        "assert(isstruct(fa_request));\npoint_index = 8;\n"
    )
    initializer = (
        "initializer_seen = point_index;\nassert(initializer_seen == 7);\n"
        "assert(isstruct(fa_request));\npoint_index = 9;\n"
        if not script_preparation else
        "function initialize()\n"
        "initializer_seen = evalin('base','point_index');\n"
        "assert(initializer_seen == 8);\n"
        "assignin('base','initializer_seen',initializer_seen);\n"
        "assignin('base','point_index',9);\nend\n"
    )
    (root / "initialize.m").write_text(initializer)
    (root / "README.md").write_text(
        "Model: workspace_loop.slx\nInitializer: initialize.m\n"
        "Preparation: choose_point(7).\n"
        "Operating point: initialized current selected operating point.\n"
        "feedback_sign = 1.\n"
    )
    engine = matlab.engine.start_matlab()
    try:
        engine.workspace["fixture_file"] = str(root / "workspace_loop.slx")
        engine.eval(
            "m='workspace_loop'; new_system(m); "
            "add_block('simulink/Sources/In1',[m '/Input']); "
            "add_block('simulink/Math Operations/Gain',[m '/Gain'],'Gain','point_index'); "
            "add_block('simulink/Sinks/Out1',[m '/Output']); "
            "add_line(m,'Input/1','Gain/1'); add_line(m,'Gain/1','Output/1'); "
            "io(1)=linio([m '/Input'],1,'openinput'); "
            "io(2)=linio([m '/Gain'],1,'openoutput'); setlinio(m,io); "
            "save_system(m,fixture_file); close_system(m,0);",
            nargout=0,
        )
    finally:
        engine.quit()
    project = discover_matlab_project(str(root))
    entries = {item.path: item.model_dump(mode="json") for item in project.entries}
    source = {"reference": "README.md", "quote": "Preparation: choose_point(7)."}
    preparations = [{
        "entry": {**entries["choose_point.m"], "arguments": [7]}, "source": source,
    }]
    conditions = {"point_index": 9, "initializer_seen": 8 if script_preparation else 7}
    if script_preparation:
        preparations.append({"entry": entries["prepare_script.m"], "source": source})
        conditions["prep_seen"] = 7
    request = LocalMatlabRequest.model_validate({
        "id": "c" * 32, "operation": "linearize", "project": project.model_dump(mode="json"),
        "inputs": {
            "model": "workspace_loop.slx", "initializer": entries["initialize.m"],
            "preparations": preparations,
            "operating_point": {
                "mode": "initialized", "source": {
                    "reference": "README.md",
                    "quote": "Operating point: initialized current selected operating point.",
                },
            },
            "feedback_sign": 1,
            "feedback_source": {"reference": "README.md", "quote": "feedback_sign = 1."},
            "conditions": conditions,
            "workspace_assertions": [
                {"path": [name], "condition": name, "source": source} for name in conditions
            ],
        },
    })
    directory = tmp_path / "linearize"
    result = actual_backend.execute(request, directory)
    assert result.valid and result.execution_status == "completed", result.diagnostics
    assert result.details["linearization"]["workspace_assertions"] == [
        {"path": [name], "condition": name, "actual": value}
        for name, value in conditions.items()
    ]
    assert discover_matlab_project(str(root)) == project
    assert actual_backend.execute(request, directory, resume=True) == result
