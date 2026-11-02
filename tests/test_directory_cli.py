import json
import subprocess
import sys

import httpx
import pytest

from flight_agent import cli
from tests.agent_helpers import draft_data, make_agent_web
from tests.helpers import serve_app


def test_directory_task_cli_help_uses_registered_locations_without_scenarios():
    result = subprocess.run(
        [sys.executable, "-m", "flight_agent", "directory-task", "propose", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--scenario" not in result.stdout
    assert "--location" in result.stdout
    assert "--output-root" in result.stdout


@pytest.mark.parametrize("existing", [False, True])
def test_directory_cli_does_not_inject_resource_selection_on_new_or_existing_requests(
    monkeypatch, capsys, existing
):
    identifier = "b" * 32
    real_client = httpx.Client

    def handler(request):
        if request.url.path == "/api/bootstrap":
            return httpx.Response(200, json={"token": "local-test-token"})
        assert request.headers["X-Flight-Token"] == "local-test-token"
        body = json.loads(request.content)
        assert body == {"message": "Explain gain margin."}
        return httpx.Response(202, json={"id": identifier, "status": "completed", "task": None})

    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    args = ["directory-task", "propose", "--request", "Explain gain margin."]
    if existing:
        args.extend(["--conversation", identifier])
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["task"] is None


def test_directory_cli_uses_authenticated_api_and_preserves_async_task_identity(
    monkeypatch, capsys
):
    calls = []
    identifier = "a" * 32
    real_client = httpx.Client

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/bootstrap":
            return httpx.Response(200, json={"token": "local-test-token"})
        assert request.headers["X-Flight-Token"] == "local-test-token"
        if request.method == "POST":
            assert json.loads(request.content)["location"] == "diagnostic_data"
            return httpx.Response(202, json={"id": identifier, "status": "planning"})
        return httpx.Response(
            200, json={"id": identifier, "resource_location": "diagnostic_data",
                       "status": "waiting_for_user"}
        )

    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert (
        cli.main(
            [
                "directory-task",
                "propose",
                "--location",
                "diagnostic_data",
                "--project-root",
                "/registered/data",
                "--request",
                "Investigate the mismatch.",
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["id"] == identifier
    assert record["status"] == "waiting_for_user"
    assert [request.method for request in calls] == ["GET", "POST", "GET"]


def test_directory_cli_confirmation_needs_explicit_consent_before_contact(monkeypatch, capsys):
    def forbidden(**kwargs):
        pytest.fail("Missing consent must be rejected before contacting the server")

    monkeypatch.setattr(cli.httpx, "Client", forbidden)
    assert (
        cli.main(
            [
                "directory-task",
                "confirm",
                "a" * 32,
                "--version",
                "1",
            ]
        )
        == 1
    )
    assert "--trusted-project" in capsys.readouterr().err


@pytest.mark.parametrize(
    "server", ["https://example.test", "http://127.0.0.1:8000/api", "http://user@localhost"]
)
def test_directory_cli_never_sends_local_paths_to_nonlocal_or_redirected_endpoints(
    server, monkeypatch, capsys
):
    def forbidden(**kwargs):
        pytest.fail("Unsafe server must be rejected before creating an HTTP client")

    monkeypatch.setattr(cli.httpx, "Client", forbidden)
    assert cli.main(["directory-task", "--server", server, "status", "a" * 32]) == 1
    assert "local" in capsys.readouterr().err.lower()


def test_default_directory_cli_uses_generic_plan_and_explicit_confirmation(tmp_path, capsys):
    app, _, calls, _ = make_agent_web(
        tmp_path,
        [
            {
                "action": "plan",
                "text": "Confirm synthetic arrays.",
                "plan": draft_data(),
            }
        ],
    )
    with serve_app(app) as server:
        assert (
            cli.main(
                [
                    "directory-task",
                    "--server",
                    server,
                    "propose",
                    "--request",
                    "Analyze synthetic arrays [0,2] and [0,0]; no acceptance.",
                ]
            )
            == 0
        )
        pending = json.loads(capsys.readouterr().out)
        assert pending["status"] == "waiting_for_user"
        assert pending["task"]["schema_version"] == "2.0"
        assert "scenario" not in pending["task"]
        assert pending["task"]["baseline"]["resources"] == []
        assert pending["task"]["baseline"]["nodes"][0]["tool"] == "offline_rmse"
        assert calls == []
        assert (
            cli.main(
                [
                    "directory-task",
                    "--server",
                    server,
                    "confirm",
                    pending["id"],
                    "--version",
                    "1",
                    "--trusted-project",
                ]
            )
            == 0
        )
        done = json.loads(capsys.readouterr().out)
        assert done["task"]["result"]["observations"][0]["value"] == pytest.approx(2**0.5)
        assert done["task"]["result"]["evaluation"] is None
        assert len(calls) == 1


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["--analysis-request", "  Existing entry 中文  ", "--criteria", "超调量 ≤10%"],
            {"analysis_request": "  Existing entry 中文  ", "evaluation_criteria": "超调量 ≤10%"},
        ),
        (
            ["--request", "Keep original text", "--criteria", "RMSE <= 2 m"],
            {"analysis_request": "Keep original text", "evaluation_criteria": "RMSE <= 2 m"},
        ),
        (
            ["--analysis-request", "Existing entry", "--location", "matlab_project"],
            {"analysis_request": "Existing entry", "location": "matlab_project"},
        ),
        (
            ["--request", "Worker request", "--location", "simulink_project"],
            {"message": "Worker request", "location": "simulink_project"},
        ),
        (
            ["--analysis-request", "Data analysis", "--location", "diagnostic_data"],
            {"analysis_request": "Data analysis", "location": "diagnostic_data"},
        ),
        (
            ["--analysis-request", "Follow up", "--conversation", "b" * 32],
            {"analysis_request": "Follow up"},
        ),
    ],
)
def test_directory_cli_structured_and_registered_resource_payloads(
    monkeypatch, capsys, arguments, expected,
):
    real_client = httpx.Client
    received = []

    def handler(request):
        if request.url.path == "/api/bootstrap":
            return httpx.Response(200, json={"token": "local-test-token"})
        received.append(json.loads(request.content))
        assert request.headers["X-Flight-Token"] == "local-test-token"
        return httpx.Response(202, json={"id": "b" * 32, "status": "waiting_for_user"})

    monkeypatch.setattr(
        cli.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert cli.main(["directory-task", "propose", *arguments]) == 0
    assert received == [expected]
    capsys.readouterr()


def test_directory_cli_request_forms_are_mutually_exclusive_before_contact(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("Conflicting flags must be rejected before contacting the server")

    monkeypatch.setattr(cli.httpx, "Client", forbidden)
    with pytest.raises(SystemExit) as rejected:
        cli.main([
            "directory-task", "propose", "--request", "old", "--analysis-request", "new",
        ])
    assert rejected.value.code == 2
