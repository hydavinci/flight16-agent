import json
import subprocess
import sys

import httpx
import pytest

from flight_agent import cli


def test_directory_task_cli_help_has_both_scenarios():
    result = subprocess.run(
        [sys.executable, "-m", "flight_agent", "directory-task", "propose", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--scenario" in result.stdout
    assert "d1" in result.stdout and "d2" in result.stdout
    assert "--output-root" in result.stdout


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
            assert json.loads(request.content)["scenario"] == "d2"
            return httpx.Response(202, json={"id": identifier, "status": "planning"})
        return httpx.Response(
            200, json={"id": identifier, "scenario": "d2", "status": "waiting_for_user"}
        )

    monkeypatch.setattr(
        cli.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert cli.main([
        "directory-task", "propose", "--scenario", "d2",
        "--project-root", "/registered/data", "--request", "Investigate the mismatch.",
    ]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["id"] == identifier
    assert record["status"] == "waiting_for_user"
    assert [request.method for request in calls] == ["GET", "POST", "GET"]


def test_directory_cli_confirmation_needs_explicit_consent_before_contact(monkeypatch, capsys):
    def forbidden(**kwargs):
        pytest.fail("Missing consent must be rejected before contacting the server")

    monkeypatch.setattr(cli.httpx, "Client", forbidden)
    assert cli.main([
        "directory-task", "confirm", "a" * 32, "--version", "1",
    ]) == 1
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
