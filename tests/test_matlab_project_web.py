import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flight_agent.verification.local_matlab import LocalMatlabBackend
from flight_agent.verification.routes import MessageRequest
from flight_agent.web import create_web_app
from tests.agent_helpers import make_agent_web, wait_for_conversation

ANALYSIS = "  Stick 阶跃；非线性闭环时域仿真；stepinfo\n保留已有入口。  "
CRITERIA = " 超调量 ≤10%；调节时间 ≤3 s（5%误差带）\n "
CANONICAL = (
    "[工程分析要求]\n"
    "  Stick 阶跃；非线性闭环时域仿真；stepinfo\n保留已有入口。  \n"
    "[工程分析要求结束]\n\n"
    "[评价指标]\n"
    " 超调量 ≤10%；调节时间 ≤3 s（5%误差带）\n \n"
    "[评价指标结束]"
)


def test_structured_requirements_keep_labelled_exact_source_text():
    request = MessageRequest(analysis_request=ANALYSIS, evaluation_criteria=CRITERIA)
    assert request.canonical_message() == CANONICAL
    assert ANALYSIS in request.canonical_message()
    assert CRITERIA in request.canonical_message()


def test_analysis_without_criteria_and_legacy_message_are_supported():
    assert MessageRequest(message=" Old client ").canonical_message() == "Old client"
    assert MessageRequest(analysis_request="仅分析").canonical_message() == (
        "[工程分析要求]\n仅分析\n[工程分析要求结束]"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"message": "  "},
        {"analysis_request": "  "},
        {"evaluation_criteria": "x"},
        {"analysis_request": "x", "evaluation_criteria": "  "},
        {"message": "same", "analysis_request": "same"},
        {"message": "x", "evaluation_criteria": "y"},
        {"message": "x", "analysis_request": "x", "evaluation_criteria": "y"},
        {"analysis_request": "x" * 4001},
        {"analysis_request": "x", "evaluation_criteria": "x" * 4001},
        {"analysis_request": "x" * 2000, "evaluation_criteria": "y" * 2000},
    ],
)
def test_invalid_or_conflicting_forms_are_rejected(fields):
    with pytest.raises(ValidationError):
        MessageRequest.model_validate(fields)


def test_combined_limit_includes_labels_and_accepts_exact_boundary():
    overhead = len("[工程分析要求]\n\n[工程分析要求结束]\n\n[评价指标]\n\n[评价指标结束]")
    fields = {"analysis_request": "a" * (4000 - overhead - 1), "evaluation_criteria": "b"}
    assert len(MessageRequest(**fields).canonical_message()) == 4000
    fields["evaluation_criteria"] = "bb"
    with pytest.raises(ValidationError, match="4000"):
        MessageRequest(**fields)


def test_structured_api_persists_canonical_source_on_create_and_followup(tmp_path):
    app, runtime, calls, _ = make_agent_web(tmp_path, [
        {"action": "clarify", "text": "Which existing entry?"},
        {"action": "clarify", "text": "Which response signal?"},
    ])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Flight-Token": client.get("/api/bootstrap").json()["token"]}
        path = "/api/conversations"
        for reply in ["Which existing entry?", "Which response signal?"]:
            response = client.post(path, headers=headers, json={
                "analysis_request": ANALYSIS, "evaluation_criteria": CRITERIA,
            })
            assert response.status_code == 202, response.text
            record = wait_for_conversation(
                client, response.json()["id"], headers,
                lambda item: item["status"] == "waiting_for_user",
            )
            assert record["messages"][-2]["content"] == CANONICAL
            assert record["messages"][-1]["content"] == reply
            path = f"/api/conversations/{record['id']}/messages"
        assert len(runtime.store.recent()) == 1
        rejected = client.post(path, headers=headers, json={
            "message": "legacy", "analysis_request": ANALYSIS,
        })
        assert rejected.status_code == 422
        assert "message" in rejected.text and "structured" in rejected.text
        assert len(runtime.store.get(record["id"]).messages) == 4
        assert calls == []


@pytest.mark.parametrize(
    ("location", "scenario", "expected"),
    [
        ("agent", None, "diagnostic_data"),
        ("worker", None, "simulink_project"),
        (None, "d1", "simulink_project"),
        ("matlab_project", None, "matlab_project"),
    ],
)
def test_explicit_resource_aliases_do_not_change(tmp_path, location, scenario, expected):
    from flight_agent.verification.compat import resolve_location

    app = create_web_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Flight-Token": client.get("/api/bootstrap").json()["token"]}
        from flight_agent.verification.resources import ResourceLocation

        locations = [
            ResourceLocation.model_validate(item)
            for item in client.get("/api/resource-locations", headers=headers).json()
        ]
        assert resolve_location(location, scenario, locations) == expected
        with pytest.raises(ValueError, match="conflicts"):
            resolve_location("matlab_project", "d1", locations)


def test_bootstrap_trust_gate_does_not_probe_or_execute(tmp_path, monkeypatch):
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Untrusted bootstrap must not spawn a probe or execute MATLAB")

    monkeypatch.setattr(LocalMatlabBackend, "_spawn", forbidden)
    monkeypatch.setattr(LocalMatlabBackend, "execute", forbidden)
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        data = client.get("/api/bootstrap").json()
    assert data["local_matlab"]["state"] == "unavailable"
    assert "MATLAB_ALLOW_TRUSTED_PROJECTS=1" in data["local_matlab"]["reason"]


@pytest.mark.parametrize("available", [True, False])
def test_bootstrap_only_runs_selected_bounded_import_probe_without_private_paths(
    tmp_path, monkeypatch, available,
):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.setenv("MATLAB_PYTHON_EXECUTABLE", "/private/engine/python")
    interpreter = tmp_path / "probe-python"
    interpreter.write_text(
        "#!/bin/sh\n"
        + ("printf '{\"available\": true}'\n" if available else
           "printf '/private/engine/python: secret import error' >&2\nexit 1\n"),
        encoding="utf-8",
    )
    interpreter.chmod(0o700)
    spawned = []

    def probe(backend, argument, *, cwd, log):
        assert backend.interpreter == "/private/engine/python"
        assert argument == "--probe"
        spawned.append(argument)
        # Run the real bounded probe lifecycle, with a test-only executable.
        return subprocess.Popen(
            [str(interpreter)], cwd=cwd, stdout=log, stderr=log,
            text=True, start_new_session=True,
        )

    monkeypatch.setattr(LocalMatlabBackend, "_spawn", probe)
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        data = client.get("/api/bootstrap").json()
        assert data["local_matlab"]["state"] == ("available" if available else "unavailable")
        assert "license" in data["local_matlab"]["reason"] if available else (
            "Engine" in data["local_matlab"]["reason"]
        )
    assert spawned == ["--probe"]
    assert "/private/" not in json.dumps(data)
    assert "secret import error" not in json.dumps(data)
