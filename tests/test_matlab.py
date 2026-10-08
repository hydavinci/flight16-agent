import importlib
import os
import signal
import subprocess
import time

import pytest

from flight_agent.models import Parameters


def matlab_module():
    try:
        return importlib.import_module("flight_agent.matlab")
    except ModuleNotFoundError:
        pytest.fail("MATLAB Engine subprocess adapter has not been implemented")


def test_real_subprocess_bridge_with_test_engine(project, parameter_data, fake_engine, monkeypatch):
    marker = project / "quit.txt"
    monkeypatch.setenv("TEST_QUIT_FILE", str(marker))
    runner = matlab_module().MatlabRunner(project, timeout_seconds=10)
    result = runner.run(Parameters.model_validate(parameter_data))
    assert result.metrics["dynamic_pressure"].value == pytest.approx(245.0)
    assert result.metrics["wing_loading"].value == pytest.approx(122.583125)
    assert result.matlab_version == "test-engine"
    assert marker.read_text() == "quit"
    assert "test MATLAB output" in runner.log


@pytest.mark.parametrize(
    "mode,fragment",
    [
        ("startup_error", "license"),
        ("execution_error", "solver"),
        ("malformed", "JSON"),
        ("wrong_type", "string"),
        ("nan", "finite"),
        ("mismatch", "condition"),
    ],
)
def test_engine_errors_are_explicit(
    project, parameter_data, fake_engine, monkeypatch, mode, fragment
):
    monkeypatch.setenv("TEST_ENGINE_MODE", mode)
    module = matlab_module()
    with pytest.raises(module.MatlabError, match=fragment):
        module.MatlabRunner(project, timeout_seconds=10).run(
            Parameters.model_validate(parameter_data)
        )


def test_missing_engine_never_returns_demo_results(project, parameter_data, monkeypatch):
    module = matlab_module()
    package = project / "missing-engine" / "matlab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    with pytest.raises(module.MatlabError, match="MATLAB Engine"):
        module.MatlabRunner(project).run(Parameters.model_validate(parameter_data))


def test_timeout_stops_owned_process_group(project, parameter_data, fake_engine, monkeypatch):
    module = matlab_module()
    marker = project / "child.pid"
    monkeypatch.setenv("TEST_ENGINE_MODE", "block")
    monkeypatch.setenv("TEST_CHILD_PID_FILE", str(marker))
    started = time.monotonic()
    with pytest.raises(module.MatlabError, match="timed out"):
        module.MatlabRunner(project, timeout_seconds=1).run(
            Parameters.model_validate(parameter_data)
        )
    assert time.monotonic() - started < 6
    assert marker.exists(), "The test must reach MATLAB execution before the deadline"
    pid = int(marker.read_text())
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert not state or state.startswith("Z"), f"Owned child {pid} still running: {state}"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_rejected(project, timeout):
    with pytest.raises(ValueError):
        matlab_module().MatlabRunner(project, timeout_seconds=timeout)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group lifecycle")
def test_timeout_kills_stubborn_descendant_with_closed_pipes(
    project, parameter_data, fake_engine, monkeypatch
):
    module = matlab_module()
    marker = project / "stubborn.pid"
    monkeypatch.setenv("TEST_ENGINE_MODE", "stubborn")
    monkeypatch.setenv("TEST_CHILD_PID_FILE", str(marker))
    try:
        with pytest.raises(module.MatlabError, match="timed out"):
            module.MatlabRunner(project, timeout_seconds=1).run(
                Parameters.model_validate(parameter_data)
            )
        assert marker.exists(), "The stubborn child must install its signal handler first"
        pid = int(marker.read_text())
        deadline = time.monotonic() + 1
        while True:
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
            ).stdout.strip()
            if not state or state.startswith("Z") or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert not state or state.startswith("Z"), f"Owned child {pid} still running: {state}"
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group lifecycle")
@pytest.mark.parametrize("adapter", ["cli", "upload"])
def test_failed_worker_cleans_up_owned_descendants(
    project, parameter_data, fake_engine, monkeypatch, adapter
):
    from flight_agent.upload_engine import execute_upload
    from flight_agent.upload_models import UploadRequest

    module = matlab_module()
    marker = project / "crashed-worker-child.pid"
    monkeypatch.setenv("TEST_ENGINE_MODE", "crash_with_child")
    monkeypatch.setenv("TEST_CHILD_PID_FILE", str(marker))
    try:
        with pytest.raises(module.MatlabError, match="failed"):
            if adapter == "cli":
                module.MatlabRunner(project, timeout_seconds=10).run(
                    Parameters.model_validate(parameter_data)
                )
            else:
                execute_upload(
                    project / "upload-job",
                    UploadRequest(
                        filename="calculation.m",
                        source="result=1;",
                        kind="script",
                        parameters={},
                        trusted=True,
                        timeout_seconds=10,
                    ),
                )
        assert marker.exists(), "The worker must exit after starting its descendant"
        pid = int(marker.read_text())
        deadline = time.monotonic() + 1
        while True:
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
            ).stdout.strip()
            if not state or state.startswith("Z") or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert not state or state.startswith("Z"), f"Owned child {pid} still running: {state}"
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.matlab
@pytest.mark.skipif(os.environ.get("RUN_MATLAB_TESTS") != "1", reason="Live MATLAB is opt-in")
def test_live_matlab_demo(repository_root, parameter_data):
    module = matlab_module()
    result = module.MatlabRunner(repository_root).run(Parameters.model_validate(parameter_data))
    assert result.valid is True
    assert result.matlab_version != "test-engine"
    assert result.metrics["dynamic_pressure"].value == pytest.approx(245.0)
    assert result.metrics["wing_loading"].value == pytest.approx(122.583125)
    assert result.metrics["required_lift_coefficient"].value == pytest.approx(0.5003392857142857)
    assert result.metrics["thrust_to_weight"].value == pytest.approx(0.3059148638933785)
