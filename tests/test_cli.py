import importlib
import json
import subprocess
import sys

import pytest

from tests.helpers import ScriptedModel, successful_responses


def cli_module():
    try:
        return importlib.import_module("flight_agent.cli")
    except ModuleNotFoundError:
        pytest.fail("CLI has not been implemented")


@pytest.fixture
def clear_model_env(monkeypatch):
    for name in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_doctor_never_contacts_model_or_exposes_key(project, monkeypatch, capsys):
    module = cli_module()
    monkeypatch.setenv("LLM_API_KEY", "do-not-print-secret")
    result = module.main(["doctor", "--project-root", str(project)])
    output = capsys.readouterr()
    assert result == 1
    assert "MATLAB Engine" in output.out
    assert "do-not-print-secret" not in output.out + output.err


def test_missing_model_configuration_is_explicit(project, parameter_data, clear_model_env, capsys):
    (project / "input.json").write_text(json.dumps(parameter_data))
    code = cli_module().main(["analyze", "input.json", "--project-root", str(project)])
    assert code == 1
    assert "LLM_" in capsys.readouterr().err


def test_cli_rejects_bad_input_before_model_call(project, parameter_data, capsys):
    parameter_data["mass_kg"] = -5
    (project / "input.json").write_text(json.dumps(parameter_data))
    code = cli_module().main(["analyze", "input.json", "--project-root", str(project)])
    assert code == 1
    assert "mass_kg" in capsys.readouterr().err
    assert not (project / "runs").exists()


def test_cli_end_to_end_with_test_engine(project, parameter_data, fake_engine, monkeypatch, capsys):
    module = cli_module()
    (project / "input.json").write_text(json.dumps(parameter_data))
    monkeypatch.setattr(
        module, "create_model", lambda: ScriptedModel(responses=successful_responses())
    )
    code = module.main(["analyze", "input.json", "--project-root", str(project)])
    assert code == 0
    report = next((project / "runs").glob("*/report.md"))
    assert str(report) in capsys.readouterr().out
    assert "判定状态：`pass`" in report.read_text()
    assert "MATLAB" in report.read_text()


def test_dotenv_is_loaded_from_explicit_project_root(
    project, clear_model_env, monkeypatch, parameter_data, capsys
):
    module = cli_module()
    (project / ".env").write_text("LLM_MODEL=test-model\nLLM_BASE_URL=http://127.0.0.1:9/v1\n")
    monkeypatch.chdir(project.parent)
    module.main(["doctor", "--project-root", str(project)])
    output = capsys.readouterr().out
    assert "LLM_MODEL: configured" in output


def test_model_configuration_rejects_implicit_destination(clear_model_env, monkeypatch):
    try:
        module = importlib.import_module("flight_agent.config")
    except ModuleNotFoundError:
        pytest.fail("Explicit model endpoint configuration has not been implemented")
    monkeypatch.setenv("LLM_API_KEY", "local")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        module.create_model()


@pytest.mark.parametrize("command", ["web", "worker"])
def test_web_commands_have_real_cli_help(command):
    result = subprocess.run(
        [sys.executable, "-m", "flight_agent", command, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--port" in result.stdout
    assert "--project-root" in result.stdout
