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


@pytest.mark.parametrize("enabled", [False, True])
def test_web_native_directory_picker_is_explicit(project, monkeypatch, enabled):
    import uvicorn

    from flight_agent import web

    calls = []
    app = object()

    def create(root, **kwargs):
        calls.append((root, kwargs))
        return app

    monkeypatch.setattr(web, "create_web_app", create)
    monkeypatch.setattr(
        uvicorn, "run",
        lambda application, **kwargs: calls.append((application, kwargs)),
    )
    args = ["web", "--project-root", str(project)]
    if enabled:
        args.append("--native-directory-picker")

    assert cli_module().main(args) == 0
    assert calls[0] == (project, {"native_directory_picker": enabled})
    assert calls[1] == (app, {"host": "127.0.0.1", "port": 8000, "workers": 1})


def test_directory_task_rejects_scenario_alias(monkeypatch, capsys):
    module = cli_module()
    monkeypatch.setattr(
        module, "directory_task", lambda _: pytest.fail("Legacy input reached execution")
    )
    with pytest.raises(SystemExit) as exc:
        module.main(["directory-task", "propose", "--request", "Analyze.", "--scenario", "d1"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --scenario d1" in capsys.readouterr().err


@pytest.mark.parametrize("structured", [False, True])
def test_directory_task_sends_only_generic_request_fields(tmp_path, monkeypatch, capsys, structured):
    from fastapi.testclient import TestClient

    from tests.agent_helpers import make_agent_web

    module = cli_module()
    app, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "answer", "text": "No execution."}]
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    form = ["--analysis-request", "Explain RMSE.", "--criteria", "No acceptance decision."] \
        if structured else ["--request", "Explain RMSE."]
    assert module.main(["directory-task", "propose", *form]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "completed"
    assert record["resource_location"] is None
    assert runtime.store.get(record["id"]).task is None
    assert not calls


@pytest.mark.parametrize("location_count", [0, 1, 2])
def test_directory_task_leaves_location_selection_to_registered_server_resources(
    tmp_path, monkeypatch, capsys, location_count,
):
    from fastapi.testclient import TestClient

    from flight_agent.runtime.resources import ResourceReference
    from flight_agent.verification.resources import ResourceAdapter
    from tests.agent_helpers import make_agent_web

    module = cli_module()
    app, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "answer", "text": "No execution."}]
    )
    for index in range(location_count):
        runtime.resources.register(ResourceAdapter(
            name=f"registered-{index}", kind="data", location=f"host-{index}",
            inspect=lambda locator, i=index: ResourceReference(
                id="inputs", kind="data", location=f"host-{i}",
                locator=locator, fingerprint="a" * 64,
            ),
            verify=lambda _: None,
            browse=lambda _: pytest.fail("CLI proposal must not browse resources"),
        ))
    client = TestClient(app, base_url="http://127.0.0.1")
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)

    assert module.main([
        "directory-task", "propose", "--project-root", str(tmp_path),
        "--analysis-request", "Explain the registered inputs.",
    ]) == 0

    record = json.loads(capsys.readouterr().out)
    assert record["project_root"] == str(tmp_path)
    assert record["resource_location"] == ("registered-0" if location_count == 1 else None)
    assert record["status"] == ("completed" if location_count == 1 else "waiting_for_user")
    assert not calls
