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


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_root", "/another/project"),
        ("id", "b" * 32),
        ("entry_nargout", 0),
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
