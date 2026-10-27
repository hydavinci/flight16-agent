import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flight_agent.runtime.contracts import fingerprint
from flight_agent.verification.matlab_contracts import (
    LocalMatlabRequest,
    ProjectExecutionInput,
    SignalSelection,
)
from flight_agent.verification.matlab_projects import discover_matlab_project
from tests.matlab_project_helpers import prepared_project


@pytest.fixture
def local_backend(monkeypatch):
    try:
        module = importlib.import_module("flight_agent.verification.local_matlab")
    except ModuleNotFoundError:
        pytest.fail("LocalMatlabBackend has not been implemented")
    root = Path(__file__).resolve().parents[1]
    fake = Path(__file__).parent / "fixtures" / "fake_local_matlab"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(fake), str(root / "src")]))
    monkeypatch.setenv("MATLAB_PYTHON_EXECUTABLE", sys.executable)
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    return module.LocalMatlabBackend.from_environment()


@pytest.fixture
def local_request(tmp_path):
    root = prepared_project(tmp_path / "source")
    (root / "analyze.m").write_text(
        "function result = analyze(options)\nresult = options;\nend\n", encoding="utf-8"
    )
    project = discover_matlab_project(str(root))
    entry = next(item for item in project.entries if item.path == "analyze.m")
    entry = entry.model_copy(update={"arguments": [{"condition": "cruise"}]})
    return LocalMatlabRequest(
        id="a" * 32,
        operation="execute_project",
        project=project,
        inputs=ProjectExecutionInput(
            mode="entry",
            entry=entry,
            initializer=next(item for item in project.entries if item.path == "initialize.m"),
            input_signal=SignalSelection(source="return", path=["command"], unit="deg"),
            response_signal=SignalSelection(source="return", path=["response"], unit="deg/s"),
            conditions={"condition": "cruise"},
        ),
    )


def test_probe_selected_interpreter_without_starting_engine(local_backend, tmp_path, monkeypatch):
    marker = tmp_path / "probe-starts"
    monkeypatch.setenv("TEST_LOCAL_STARTS", str(marker))
    assert local_backend.interpreter == sys.executable
    assert local_backend.availability().state == "available"
    assert not marker.exists()


def test_probe_respects_default_and_missing_interpreter(local_backend, monkeypatch):
    module = importlib.import_module("flight_agent.verification.local_matlab")
    monkeypatch.delenv("MATLAB_PYTHON_EXECUTABLE")
    assert module.LocalMatlabBackend.from_environment().interpreter == sys.executable
    monkeypatch.setenv("MATLAB_PYTHON_EXECUTABLE", "/not/an/installed/python")
    unavailable = module.LocalMatlabBackend.from_environment().availability()
    assert unavailable.state == "unavailable"
    assert "interpreter" in unavailable.reason.lower()


@pytest.mark.parametrize("missing", ["engine", "worker"])
def test_probe_checks_selected_interpreter_imports(local_backend, tmp_path, monkeypatch, missing):
    if missing == "engine":
        package = tmp_path / "matlab"
        package.mkdir()
        (package / "__init__.py").write_text("")
        monkeypatch.setenv(
            "PYTHONPATH", os.pathsep.join([str(tmp_path), str(Path("src").resolve())])
        )
    else:
        monkeypatch.delenv("PYTHONPATH")
        monkeypatch.setenv("MATLAB_PYTHON_EXECUTABLE", "/usr/bin/python3")
        local_backend = type(local_backend).from_environment()
    unavailable = local_backend.availability()
    assert unavailable.state == "unavailable"
    assert unavailable.reason


def test_execution_records_original_identity_and_snapshot_evidence(
    local_backend, local_request, tmp_path, monkeypatch
):
    for prefix in ("LLM_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "MATLAB_WORKER_"):
        monkeypatch.setenv(prefix + "SECRET", "do-not-forward")
    directory = tmp_path / "run"
    result = local_backend.execute(local_request, directory)
    assert result.execution_status == "completed", result.diagnostics
    assert result.valid
    assert result.id == "a" * 32
    assert result.request_sha256 == fingerprint(local_request.model_dump(mode="json"))
    assert json.loads((directory / "request.json").read_text()) == local_request.model_dump(
        mode="json"
    )
    state = json.loads((directory / "state.json").read_text())
    assert state["id"] == result.id and state["request_sha256"] == result.request_sha256
    assert state["status"] == "completed" and state["pid"] > 0
    calls = json.loads((directory / "calls.json").read_text())
    assert calls["name"] == "flight_agent_run_project"
    assert calls["nargout"] == 0 and calls["request_path"] == str(directory / "execution.json")
    assert calls["snapshot_root"] == str(directory / "project")
    assert calls["inputs"] == local_request.inputs.model_dump(mode="json")
    assert (directory / "engine-starts.txt").read_text() == "1"
    assert (directory / "engine-quits.txt").read_text() == "1"
    assert {item.path for item in result.artifacts} == {
        "raw.mat",
        "series.json",
        "runtime-metadata.json",
        "metadata.json",
        "matlab.log",
    }
    assert result.details["series"]["path"] == "series.json"
    assert result.details["series"] in [item.model_dump(mode="json") for item in result.artifacts]
    assert json.loads((directory / "series.json").read_text())["response"]["values"] == [0, 2, 3]
    assert discover_matlab_project(local_request.project.root) == local_request.project
    assert not (Path(local_request.project.root) / "raw.mat").exists()


def test_model_mode_keeps_initializer_and_existing_settings(local_backend, local_request, tmp_path):
    data = local_request.model_dump(mode="json")
    data["inputs"].update(mode="simulink", model="plant.slx", entry=None)
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), tmp_path / "model")
    assert result.valid, result.diagnostics
    metadata = json.loads((tmp_path / "model" / "metadata.json").read_text())
    assert metadata["settings"] == {"StopTime": "12", "Solver": "ode45"}
    assert metadata["matlab_version"] == "test-only-engine"


def test_execution_failure_includes_bounded_nested_matlab_causes(
    local_backend, local_request, tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_LOCAL_MODE", "nested_execution_error")
    directory = tmp_path / "nested-error"
    result = local_backend.execute(local_request, directory)
    assert result.execution_status == "failed" and not result.valid
    diagnostic = "\n".join(result.diagnostics)
    assert "Error due to multiple causes" in diagnostic
    assert "specific_missing_gain" in diagnostic
    assert len(diagnostic) <= 6000
    assert result.details["engine_closed"] is True
    assert "specific_missing_gain" in (directory / "matlab.log").read_text()
    assert local_backend.execute(local_request, directory, resume=True) == result
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_resume_does_not_start_a_second_engine(local_backend, local_request, tmp_path):
    directory = tmp_path / "run"
    first = local_backend.execute(local_request, directory)
    second = local_backend.execute(local_request, directory, resume=True)
    assert second == first
    assert (directory / "engine-starts.txt").read_text() == "1"
    with pytest.raises(ValueError, match="already|resume"):
        local_backend.execute(local_request, directory)


@pytest.mark.parametrize(
    "mode,status,fragment",
    [
        ("startup_error", "failed", "license"),
        ("execution_error", "failed", "solver"),
        ("quit_error", "unknown", "cleanup"),
        ("missing_receipt", "unknown", "receipt"),
        ("malformed", "unknown", "output"),
        ("oversized", "interrupted", "limit"),
        ("wrong_identity", "unknown", "identity"),
        ("missing_artifact", "unknown", "artifact"),
        ("artifact_symlink", "unknown", "symlink"),
        ("no_series", "unknown", "series"),
        ("bad_series", "unknown", "series"),
        ("bad_metadata", "unknown", "metadata"),
        ("wrong_unit", "unknown", "unit"),
        ("wrong_signal", "unknown", "identity"),
        ("no_evidence", "unknown", "evidence"),
        ("log_flood", "interrupted", "limit"),
        ("artifact_flood", "interrupted", "limit"),
    ],
)
def test_failures_never_become_success_or_redispatch(
    local_backend, local_request, tmp_path, monkeypatch, mode, status, fragment
):
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    directory = tmp_path / "run"
    first = local_backend.execute(local_request, directory)
    assert first.execution_status == status, first.diagnostics
    assert not first.valid
    assert fragment in " ".join(first.diagnostics).lower()
    second = local_backend.execute(local_request, directory, resume=True)
    assert second == first
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_timeout_stops_only_owned_worker_group(local_backend, local_request, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LOCAL_MODE", "block")
    request = local_request.model_copy(update={"timeout_seconds": 2})
    directory = tmp_path / "run"
    result = local_backend.execute(request, directory)
    assert result.execution_status == "interrupted", result.diagnostics
    assert "timed out" in " ".join(result.diagnostics)
    pid = int((directory / "descendant.pid").read_text())
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert not state or state.startswith("Z")
    assert local_backend.execute(request, directory, resume=True) == result
    assert (directory / "engine-starts.txt").read_text() == "1"


@pytest.mark.parametrize(
    "damage",
    [
        "absent",
        "running",
        "bad_state",
        "lost_receipt",
        "changed_receipt",
        "changed_artifact",
        "changed_result",
    ],
)
def test_restart_without_verified_terminal_state_never_dispatches(
    local_backend, local_request, tmp_path, damage
):
    directory = tmp_path / "run"
    if damage != "absent":
        local_backend.execute(local_request, directory)
        if damage == "running":
            state = json.loads((directory / "state.json").read_text())
            state.update(status="running", result_sha256=None, receipt_sha256=None)
            (directory / "state.json").write_text(json.dumps(state))
            (directory / "receipt.json").unlink()
            (directory / "result.json").unlink()
        elif damage == "bad_state":
            (directory / "state.json").write_text("{}")
        elif damage == "lost_receipt":
            (directory / "receipt.json").unlink()
        elif damage == "changed_receipt":
            (directory / "receipt.json").write_text("{}")
        elif damage == "changed_result":
            (directory / "result.json").write_text("{}")
        else:
            (directory / "raw.mat").write_bytes(b"changed")
    result = local_backend.execute(local_request, directory, resume=True)
    assert result.id == local_request.id
    assert result.execution_status == "unknown" and not result.valid
    if damage == "absent":
        assert not (directory / "engine-starts.txt").exists()
    else:
        assert (directory / "engine-starts.txt").read_text() == "1"


def test_restart_reconciles_receipt_after_parent_lost_terminal_write(
    local_backend, local_request, tmp_path
):
    directory = tmp_path / "run"
    first = local_backend.execute(local_request, directory)
    state = json.loads((directory / "state.json").read_text())
    state.update(status="running", result_sha256=None, receipt_sha256=None)
    (directory / "state.json").write_text(json.dumps(state))
    (directory / "result.json").unlink()
    assert local_backend.execute(local_request, directory, resume=True) == first
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_resume_rejects_different_canonical_request(local_backend, local_request, tmp_path):
    directory = tmp_path / "run"
    local_backend.execute(local_request, directory)
    changed = local_request.model_copy(update={"id": "b" * 32})
    result = local_backend.execute(changed, directory, resume=True)
    assert result.execution_status == "unknown"
    assert "identity" in " ".join(result.diagnostics).lower()
    assert (directory / "engine-starts.txt").read_text() == "1"


@pytest.mark.parametrize("mode", ["helper_shadow", "function_shadow"])
def test_exact_resolution_required(local_backend, local_request, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    result = local_backend.execute(local_request, tmp_path / "run")
    assert not result.valid
    assert "resol" in " ".join(result.diagnostics).lower()


def test_actual_millisecond_time_is_converted_not_relabeled(
    local_backend, local_request, tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_LOCAL_MODE", "milliseconds")
    result = local_backend.execute(local_request, tmp_path / "run")
    assert result.valid, result.diagnostics
    series = json.loads((tmp_path / "run" / "series.json").read_text())
    assert series["response"]["time"] == [0, 0.001, 0.002]
    assert series["response"]["time_unit"] == "s"


@pytest.mark.parametrize("mode", ["raw_only", "return_only"])
def test_optional_selectors_allow_genuine_execution_artifacts(
    local_backend, local_request, tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    data = local_request.model_dump(mode="json")
    data["inputs"].update(input_signal=None, response_signal=None)
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), tmp_path / "run")
    assert result.valid, result.diagnostics
    assert "series" not in result.details
    assert any(item.path == "raw.mat" for item in result.artifacts)


def test_changed_source_is_rejected_before_engine_start(local_backend, local_request, tmp_path):
    (Path(local_request.project.root) / "initialize.m").write_text("changed")
    result = local_backend.execute(local_request, tmp_path / "run")
    assert not result.valid
    assert "changed" in " ".join(result.diagnostics).lower()
    assert not (tmp_path / "run" / "engine-starts.txt").exists()


def test_permission_is_required_even_with_engine_installed(
    local_backend, local_request, tmp_path, monkeypatch
):
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
    assert local_backend.availability().state == "unavailable"
    result = local_backend.execute(local_request, tmp_path / "run")
    assert result.execution_status == "failed"
    assert "MATLAB_ALLOW_TRUSTED_PROJECTS=1" in " ".join(result.diagnostics)
    assert not (tmp_path / "run" / "engine-starts.txt").exists()


def test_unselected_series_cannot_be_published_as_stepinfo_evidence(
    local_backend, local_request, tmp_path
):
    data = local_request.model_dump(mode="json")
    data["inputs"].update(input_signal=None, response_signal=None)
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), tmp_path / "run")
    assert not result.valid
    assert "selection" in " ".join(result.diagnostics).lower()


@pytest.mark.parametrize("mode", ["probe_block", "probe_flood"])
def test_readiness_probe_is_bounded_without_engine_start(
    local_backend, tmp_path, monkeypatch, mode
):
    module = importlib.import_module("flight_agent.verification.local_matlab")
    monkeypatch.setattr(module, "PROBE_SECONDS", 0.5)
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    marker = tmp_path / "starts"
    monkeypatch.setenv("TEST_LOCAL_STARTS", str(marker))
    started = time.monotonic()
    availability = local_backend.availability()
    assert availability.state == "unavailable"
    assert "limit" in availability.reason
    assert time.monotonic() - started < 4
    assert not marker.exists()


def test_resume_is_offline_and_does_not_probe_or_require_execution_permission(
    local_backend, local_request, tmp_path, monkeypatch
):
    directory = tmp_path / "run"
    first = local_backend.execute(local_request, directory)
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
    offline = type(local_backend)("/not/an/interpreter")
    assert offline.execute(local_request, directory, resume=True) == first


def test_unsupported_signature_stops_before_dispatch(local_backend, local_request, tmp_path):
    root = Path(local_request.project.root)
    (root / "analyze.m").write_text("function varargout = analyze(varargin)\nend\n")
    project = discover_matlab_project(str(root))
    data = local_request.model_dump(mode="json")
    data["project"] = project.model_dump(mode="json")
    data["inputs"]["entry"] = next(
        item.model_dump(mode="json") for item in project.entries if item.path == "analyze.m"
    )
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), tmp_path / "run")
    assert result.execution_status == "failed"
    assert "signature" in " ".join(result.diagnostics).lower()
    assert not (tmp_path / "run" / "engine-starts.txt").exists()


@pytest.mark.parametrize("role", ["entry", "initializer"])
@pytest.mark.parametrize("arguments", [[], [{"condition": "cruise"}]])
def test_fixed_multi_output_functions_keep_declared_arity(
    local_backend, local_request, tmp_path, role, arguments
):
    root = Path(local_request.project.root)
    name = "analyze" if role == "entry" else "initialize"
    (root / f"{name}.m").write_text(
        f"function [result, info] = {name}(options)\n"
        "if nargin == 0\noptions = struct;\nend\nresult = 42;\ninfo = options;\nend\n"
    )
    project = discover_matlab_project(str(root))
    data = local_request.model_dump(mode="json")
    data["project"] = project.model_dump(mode="json")
    selected = next(item for item in project.entries if item.path == f"{name}.m")
    data["inputs"][role] = selected.model_copy(update={"arguments": arguments}).model_dump(
        mode="json"
    )
    directory = tmp_path / "run"
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), directory)
    assert result.valid, result.diagnostics
    execution = json.loads((directory / "execution.json").read_text())
    assert execution[role + "_nargin"] == 1
    assert execution[role + "_nargout"] == 2
    assert execution[role + "_arguments"] == [json.dumps(value) for value in arguments]


def test_optional_input_proof_cannot_be_forged(local_backend, local_request, tmp_path):
    root = Path(local_request.project.root)
    (root / "initialize.m").write_text("function initialize(required)\nend\n")
    project = discover_matlab_project(str(root))
    data = local_request.model_dump(mode="json")
    data["project"] = project.model_dump(mode="json")
    data["project"]["settings"]["function_defaults"] = [
        {
            "path": "initialize.m",
            "location": "initialize.m:2",
            "name": "argument_counts",
            "value": [0],
        }
    ]
    data["inputs"]["initializer"] = next(
        item.model_dump(mode="json") for item in project.entries if item.path == "initialize.m"
    )
    directory = tmp_path / "run"
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), directory)
    assert result.execution_status == "failed" and not result.valid
    assert not (directory / "engine-starts.txt").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_root", "/another/project"),
        ("id", "b" * 32),
        ("entry_nargout", 0),
        ("entry_nargin", 0),
        ("initializer_nargin", 1),
    ],
)
def test_worker_revalidates_execution_locators_before_engine_start(
    local_backend, local_request, tmp_path, monkeypatch, field, value
):
    original_spawn = local_backend._spawn

    def corrupt_then_spawn(argument, *, cwd, log):
        if argument != "--probe":
            path = cwd / "execution.json"
            data = json.loads(path.read_text())
            data[field] = value
            path.write_text(json.dumps(data))
        return original_spawn(argument, cwd=cwd, log=log)

    monkeypatch.setattr(local_backend, "_spawn", corrupt_then_spawn)
    directory = tmp_path / "run"
    result = local_backend.execute(local_request, directory)
    assert not result.valid
    assert not (directory / "engine-starts.txt").exists()


def test_uncertain_owned_cleanup_is_durable_not_reconciled_to_success(
    local_backend, local_request, tmp_path, monkeypatch
):
    module = importlib.import_module("flight_agent.verification.local_matlab")
    original_stop = module._stop_worker

    def uncertain_stop(process, *, label):
        stopped = original_stop(process, label=label)
        if label == "Local MATLAB worker":
            return stopped._replace(error="termination cannot be confirmed")
        return stopped

    monkeypatch.setattr(module, "_stop_worker", uncertain_stop)
    directory = tmp_path / "run"
    result = local_backend.execute(local_request, directory)
    assert result.execution_status == "unknown" and not result.valid
    assert "cleanup" in " ".join(result.diagnostics).lower()
    assert local_backend.execute(local_request, directory, resume=True) == result
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_parent_interruption_during_cleanup_cannot_resurrect_success(
    local_backend, local_request, tmp_path, monkeypatch
):
    module = importlib.import_module("flight_agent.verification.local_matlab")
    original_stop = module._stop_worker

    def interrupt_cleanup(process, *, label):
        stopped = original_stop(process, label=label)
        if label == "Local MATLAB worker":
            raise KeyboardInterrupt("cleanup interrupted before confirmation")
        return stopped

    directory = tmp_path / "run"
    with monkeypatch.context() as interrupted:
        interrupted.setattr(module, "_stop_worker", interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt):
            local_backend.execute(local_request, directory)
    result = local_backend.execute(local_request, directory, resume=True)
    assert result.execution_status == "unknown" and not result.valid
    assert "interrupt" in " ".join(result.diagnostics).lower()
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_restart_preserves_uncertainty_if_final_checkpoint_write_was_lost(
    local_backend, local_request, tmp_path, monkeypatch
):
    module = importlib.import_module("flight_agent.verification.local_matlab")
    original_stop = module._stop_worker

    def uncertain_stop(process, *, label):
        stopped = original_stop(process, label=label)
        if label == "Local MATLAB worker":
            return stopped._replace(error="termination uncertain")
        return stopped

    monkeypatch.setattr(module, "_stop_worker", uncertain_stop)
    directory = tmp_path / "run"
    first = local_backend.execute(local_request, directory)
    assert first.execution_status == "unknown"
    state = json.loads((directory / "state.json").read_text())
    state.update(status="running", result_sha256=None, receipt_sha256=None)
    (directory / "state.json").write_text(json.dumps(state))
    assert local_backend.execute(local_request, directory, resume=True) == first
    assert (directory / "engine-starts.txt").read_text() == "1"


def test_receipt_without_confirmed_owned_exit_is_not_enough(local_backend, local_request, tmp_path):
    directory = tmp_path / "run"
    local_backend.execute(local_request, directory)
    state = json.loads((directory / "state.json").read_text())
    state.update(
        status="running", worker_exit_confirmed=False, result_sha256=None, receipt_sha256=None
    )
    (directory / "state.json").write_text(json.dumps(state))
    (directory / "result.json").unlink()
    result = local_backend.execute(local_request, directory, resume=True)
    assert result.execution_status == "unknown" and not result.valid
    assert "unconfirmed worker termination" in " ".join(result.diagnostics).lower()
    assert (directory / "engine-starts.txt").read_text() == "1"


@pytest.mark.parametrize("mode", ["entry", "simulink"])
def test_runtime_metadata_preserves_exact_canonical_json(
    local_backend, local_request, tmp_path, mode
):
    data = local_request.model_dump(mode="json")
    shapes = {"null": None, "empty": [], "singleton": [7], "nested": [[None], [], [1, 2]]}
    data["inputs"]["conditions"] = shapes
    data["inputs"]["entry"]["arguments"] = [shapes]
    if mode == "simulink":
        data["inputs"].update(mode="simulink", model="plant.slx", entry=None)
    request = LocalMatlabRequest.model_validate(data)
    directory = tmp_path / "run"
    result = local_backend.execute(request, directory)
    assert result.valid, result.diagnostics
    metadata = json.loads((directory / "metadata.json").read_text())
    runtime = json.loads((directory / "runtime-metadata.json").read_text())
    assert "inputs" not in runtime and "conditions" not in runtime
    assert metadata["input_provenance"] == "canonical_request"
    assert metadata["inputs"] == data["inputs"]
    assert metadata["conditions"] == shapes
    assert metadata["matlab_version"] == runtime["matlab_version"] == "test-only-engine"
    assert metadata["settings"] == runtime["settings"] == {"StopTime": "12", "Solver": "ode45"}
    assert metadata["request_sha256"] == fingerprint(request.model_dump(mode="json"))
    assert local_backend.execute(request, directory, resume=True) == result


@pytest.mark.parametrize(
    "mode", ["wrong_runtime_id", "wrong_runtime_digest", "contradictory_runtime_inputs"]
)
def test_runtime_metadata_claims_are_rejected_not_overwritten(
    local_backend, local_request, tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    directory = tmp_path / "run"
    result = local_backend.execute(local_request, directory)
    assert result.execution_status == "unknown" and not result.valid
    diagnostic = " ".join(result.diagnostics).lower()
    assert ("inputs" if mode == "contradictory_runtime_inputs" else "identity") in diagnostic
    assert not (directory / "metadata.json").exists()
    assert (directory / "runtime-metadata.json").exists()
    assert local_backend.execute(local_request, directory, resume=True) == result


@pytest.mark.parametrize("role", ["entry", "initializer"])
def test_script_pfile_is_rejected_before_engine_start(local_backend, local_request, tmp_path, role):
    root = Path(local_request.project.root)
    path = "analyze.m" if role == "entry" else "initialize.m"
    (root / path).write_text("result = 42;\n")
    (root / path).with_suffix(".p").write_bytes(b"unselected P-file")
    project = discover_matlab_project(str(root))
    data = local_request.model_dump(mode="json")
    data["project"] = project.model_dump(mode="json")
    data["inputs"][role] = next(
        item.model_dump(mode="json") for item in project.entries if item.path == path
    )
    directory = tmp_path / "run"
    result = local_backend.execute(LocalMatlabRequest.model_validate(data), directory)
    assert not (directory / "engine-starts.txt").exists()
    assert result.execution_status == "failed" and not result.valid
    assert "P-file" in " ".join(result.diagnostics)
    assert not (directory / "calls.json").exists()


@pytest.fixture
def real_local_backend(monkeypatch):
    if os.environ.get("RUN_MATLAB_TESTS") != "1":
        pytest.skip("Live MATLAB is opt-in")
    assert os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") == "1"
    from flight_agent.verification.local_matlab import LocalMatlabBackend

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    return LocalMatlabBackend.from_environment()


@pytest.mark.matlab
def test_real_packaged_bridge_preserves_json_provenance(real_local_backend, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "analyze.m").write_text(
        "function result = analyze(options)\n"
        "assert(isfield(options, 'nested'));\n"
        "result = 42;\nend\n"
    )
    project = discover_matlab_project(str(root))
    shapes = {"null": None, "empty": [], "singleton": [7], "nested": [[None], [], [1, 2]]}
    request = LocalMatlabRequest(
        id="c" * 32,
        operation="execute_project",
        project=project,
        inputs=ProjectExecutionInput(
            mode="entry",
            entry=project.entries[0].model_copy(update={"arguments": [shapes]}),
            conditions=shapes,
        ),
        timeout_seconds=120,
    )
    directory = tmp_path / "run"
    result = real_local_backend.execute(request, directory)
    assert result.valid, result.diagnostics
    metadata = json.loads((directory / "metadata.json").read_text())
    runtime = json.loads((directory / "runtime-metadata.json").read_text())
    assert metadata["input_provenance"] == "canonical_request"
    assert metadata["inputs"] == request.inputs.model_dump(mode="json")
    assert metadata["conditions"] == shapes
    assert metadata["inputs"]["model"] is None
    assert metadata["inputs"]["entry"]["arguments"][0]["singleton"] == [7]
    assert metadata["matlab_version"] == runtime["matlab_version"] != "test-only-engine"
    assert runtime["settings"] == {}
    assert "inputs" not in runtime and "conditions" not in runtime
    assert json.loads((directory / "return.json").read_text()) == {"result": 42}
    assert real_local_backend.execute(request, directory, resume=True) == result
    assert discover_matlab_project(str(root)) == project
    print(f"Actual MATLAB: {metadata['matlab_version']} ({metadata['matlab_release']})")


@pytest.mark.matlab
def test_real_multi_output_optional_functions_preserve_initialization(real_local_backend, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "initialize.m").write_text(
        "function [point, selection] = initialize(folder)\n"
        "if nargin == 0\nfolder = fileparts(mfilename('fullpath'));\nend\n"
        "assert(isfolder(folder));\nassignin('base', 'compatibility_seed', 41);\n"
        "point = 41;\nselection = 999;\nend\n"
    )
    (root / "analyze.m").write_text(
        "function [result, info] = analyze(folder)\n"
        "if nargin == 0\nfolder = fileparts(mfilename('fullpath'));\nend\n"
        "assert(strcmp(folder, fileparts(mfilename('fullpath'))));\n"
        "result = evalin('base', 'compatibility_seed') + 1;\ninfo = 1000;\nend\n"
    )
    project = discover_matlab_project(str(root))
    request = LocalMatlabRequest(
        id="b" * 32,
        operation="execute_project",
        project=project,
        inputs=ProjectExecutionInput(
            mode="entry",
            entry=next(item for item in project.entries if item.path == "analyze.m"),
            initializer=next(item for item in project.entries if item.path == "initialize.m"),
        ),
        timeout_seconds=120,
    )
    directory = tmp_path / "run"
    result = real_local_backend.execute(request, directory)
    assert result.valid, result.diagnostics
    assert json.loads((directory / "return.json").read_text()) == {"result": 42}
    assert real_local_backend.execute(request, directory, resume=True) == result
    assert discover_matlab_project(str(root)) == project


@pytest.mark.matlab
def test_real_packaged_bridge_rejects_initializer_created_pfile(real_local_backend, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "initialize.m").write_text(
        "pcode(fullfile(fileparts(mfilename('fullpath')), 'analyze.m'), '-inplace');\n"
    )
    (root / "analyze.m").write_text(
        "marker = fopen('unintended.txt', 'w');\n"
        "fprintf(marker, 'unintended script execution');\nfclose(marker);\nresult = 42;\n"
    )
    project = discover_matlab_project(str(root))
    request = LocalMatlabRequest(
        id="d" * 32,
        operation="execute_project",
        project=project,
        inputs=ProjectExecutionInput(
            mode="entry",
            entry=next(item for item in project.entries if item.path == "analyze.m"),
            initializer=next(item for item in project.entries if item.path == "initialize.m"),
        ),
        timeout_seconds=120,
    )
    directory = tmp_path / "run"
    result = real_local_backend.execute(request, directory)
    assert (directory / "project" / "analyze.p").is_file(), result.diagnostics
    assert not (directory / "project" / "unintended.txt").exists(), result.diagnostics
    assert result.execution_status == "failed" and not result.valid
    assert "P-file" in " ".join(result.diagnostics)
    assert discover_matlab_project(str(root)) == project


@pytest.fixture
def stepinfo_request(tmp_path):
    from flight_agent.runtime.evidence import describe_evidence, publish_evidence
    from flight_agent.runtime.observations import OutputArtifact
    from flight_agent.verification.matlab_contracts import StepinfoInput

    name = "calls/" + "e" * 32 + "/published/series.json"
    series = {
        role: {
            "time": [0, 1, 2, 3, 4, 5],
            "values": values,
            "time_unit": "s",
            "unit": unit,
            "name": signal,
        }
        for role, values, unit, signal in [
            ("input", [0, 0, 1, 1, 1, 1], "N", "demand"),
            ("response", [0, 0, 0, 1, 1, 1], "m", "position"),
        ]
    }
    publish_evidence(tmp_path, name, json.dumps(series).encode())
    artifact = OutputArtifact(
        **describe_evidence(tmp_path, name).model_dump(), node_id="producer", id="series"
    )
    return LocalMatlabRequest(
        id="f" * 32,
        operation="stepinfo",
        inputs=StepinfoInput(
            series=artifact,
            settling_band=0.02,
            conditions={"empty": [], "singleton": [7], "null": None},
        ),
        input_evidence=[artifact],
    )


def test_stepinfo_uses_owned_lifecycle_and_lossless_metadata(
    local_backend, stepinfo_request, tmp_path
):
    directory = tmp_path / "metric"
    result = local_backend.execute(stepinfo_request, directory)
    assert result.execution_status == "completed" and result.valid, result.diagnostics
    assert result.details["mode"] == "stepinfo"
    assert result.details["settling_band"] == 0.02
    metadata = json.loads((directory / "metadata.json").read_text())
    runtime = json.loads((directory / "runtime-metadata.json").read_text())
    assert metadata["inputs"] == stepinfo_request.inputs.model_dump(mode="json")
    assert metadata["conditions"] == stepinfo_request.inputs.conditions
    assert "inputs" not in runtime and "conditions" not in runtime
    assert metadata["input_provenance"] == "canonical_request"
    assert (directory / "series.json").read_bytes() == (
        tmp_path / stepinfo_request.inputs.series.path
    ).read_bytes()
    assert {item.path for item in result.artifacts} >= {
        "raw.mat",
        "series.json",
        "step-response.png",
        "metadata.json",
        "runtime-metadata.json",
    }
    assert local_backend.execute(stepinfo_request, directory, resume=True) == result
    assert (directory / "engine-starts.txt").read_text() == "1"
    assert (directory / "engine-quits.txt").read_text() == "1"


@pytest.mark.parametrize(
    "mode,status",
    [
        ("invalid_step", "completed"),
        ("wrong_runtime_id", "unknown"),
        ("contradictory_runtime_inputs", "unknown"),
        ("bad_band", "unknown"),
        ("changed_step_source", "unknown"),
        ("missing_plot", "unknown"),
        ("wrong_step_signal", "unknown"),
        ("wrong_metric_unit", "unknown"),
        ("execution_error", "failed"),
    ],
)
def test_stepinfo_invalid_and_unverified_outputs_never_become_valid(
    local_backend, stepinfo_request, tmp_path, monkeypatch, mode, status
):
    monkeypatch.setenv("TEST_LOCAL_MODE", mode)
    directory = tmp_path / "metric"
    result = local_backend.execute(stepinfo_request, directory)
    assert result.execution_status == status and not result.valid, result.diagnostics
    if mode == "invalid_step":
        assert result.artifacts and result.diagnostics
        assert result.details["engine_closed"] is True
    assert local_backend.execute(stepinfo_request, directory, resume=True) == result


@pytest.mark.parametrize("damage", ["bytes", "unit", "missing", "evidence"])
def test_stepinfo_input_is_verified_before_engine(
    local_backend, stepinfo_request, tmp_path, damage
):
    from flight_agent.runtime.evidence import describe_evidence
    from flight_agent.runtime.observations import OutputArtifact

    request = stepinfo_request
    source = tmp_path / request.inputs.series.path
    if damage == "bytes":
        source.write_text("{}")
    elif damage == "missing":
        source.unlink()
    elif damage == "evidence":
        request = request.model_copy(update={"input_evidence": []})
    else:
        series = json.loads(source.read_text())
        series["response"]["time_unit"] = "ms"
        source.write_text(json.dumps(series))
        artifact = OutputArtifact(
            **describe_evidence(tmp_path, request.inputs.series.path).model_dump(),
            node_id="producer",
            id="series",
        )
        request = request.model_copy(
            update={
                "inputs": request.inputs.model_copy(update={"series": artifact}),
                "input_evidence": [artifact],
            }
        )
    directory = tmp_path / "metric"
    result = local_backend.execute(request, directory)
    assert not result.valid and result.execution_status == "failed"
    assert not (directory / "engine-starts.txt").exists()


@pytest.mark.matlab
def test_real_stepinfo_bridge_preserves_artifact_descriptor_and_legacy_default(
    real_local_backend, stepinfo_request, tmp_path
):
    import math

    from flight_agent.runtime.evidence import describe_evidence
    from flight_agent.runtime.observations import OutputArtifact

    # Owned synthetic data only: a boundary regression, not real-project acceptance.
    source = tmp_path / stepinfo_request.inputs.series.path
    samples = [index / 100 for index in range(1401)]
    data = json.loads(source.read_text())
    data["input"].update(time=samples, values=[0 if t < 1 else 1 for t in samples])
    data["response"].update(
        time=samples, values=[0 if t < 1 else 1 - math.exp(-(t - 1)) for t in samples]
    )
    source.write_text(json.dumps(data))
    artifact = OutputArtifact(
        **describe_evidence(tmp_path, stepinfo_request.inputs.series.path).model_dump(),
        node_id="producer",
        id=None,
    )
    request = stepinfo_request.model_copy(
        update={
            "inputs": stepinfo_request.inputs.model_copy(update={"series": artifact}),
            "input_evidence": [artifact],
        }
    )
    directory = tmp_path / "metric"
    result = real_local_backend.execute(request, directory)
    assert result.valid and result.execution_status == "completed", result.diagnostics
    assert result.details["settling_band"] == 0.02
    assert result.details["input_signal"] == {"name": "demand", "unit": "N"}
    assert result.details["response_signal"] == {"name": "position", "unit": "m"}
    assert result.details["metrics"]["settling_time"]["value"] > 0
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["inputs"] == request.inputs.model_dump(mode="json")
    assert metadata["inputs"]["series"]["node_id"] == "producer"
    assert metadata["inputs"]["series"]["id"] is None
    assert metadata["conditions"] == {"empty": [], "singleton": [7], "null": None}
    assert (directory / "series.json").read_bytes() == source.read_bytes()
    assert real_local_backend.execute(request, directory, resume=True) == result

    # Check the unchanged historical five-argument default in the actual language.
    import matlab.engine

    import matlab

    engine = matlab.engine.start_matlab()
    try:
        engine.cd(
            str(Path(__file__).resolve().parents[1] / "src/flight_agent/matlab_support"), nargout=0
        )
        policy = engine.jsondecode(request.inputs.steady_state.model_dump_json(), nargout=1)
        legacy = engine.feval(
            "flight_agent_step_metrics",
            matlab.double(samples),
            matlab.double(data["input"]["values"]),
            matlab.double(samples),
            matlab.double(data["response"]["values"]),
            policy,
            nargout=1,
        )
        assert legacy["valid"]
        assert legacy["details"]["settling_band"] == 0.05
        assert (
            legacy["metrics"]["settling_time"]["value"]
            < (result.details["metrics"]["settling_time"]["value"])
        )
    finally:
        engine.quit()


@pytest.mark.parametrize("running", [True, False])
def test_completed_invalid_stepinfo_requires_confirmed_owned_exit_on_recovery(
    local_backend, stepinfo_request, tmp_path, monkeypatch, running
):
    monkeypatch.setenv("TEST_LOCAL_MODE", "invalid_step")
    directory = tmp_path / "metric"
    first = local_backend.execute(stepinfo_request, directory)
    assert first.execution_status == "completed" and not first.valid
    state = json.loads((directory / "state.json").read_text())
    state["worker_exit_confirmed"] = False
    if running:
        state.update(status="running", result_sha256=None, receipt_sha256=None)
    (directory / "state.json").write_text(json.dumps(state))
    result = local_backend.execute(stepinfo_request, directory, resume=True)
    assert result.execution_status == "unknown", result.diagnostics
    assert "unconfirmed" in " ".join(result.diagnostics).lower()
    assert (directory / "engine-starts.txt").read_text() == "1"
