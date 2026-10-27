import hashlib
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from flight_agent.web import create_web_app
from tests.agent_helpers import (
    delivery_draft,
    make_agent_web,
    peak_current_draft,
    register_peak_current,
    wait_for_conversation,
)
from tests.helpers import tool_message

API = "/api/conversations"


def ready(client, identifier, headers, status):
    return wait_for_conversation(
        client,
        identifier,
        headers,
        lambda record: record["status"] == status,
    )


def confirm(client, identifier, headers, version):
    response = client.post(
        f"{API}/{identifier}/confirm",
        headers=headers,
        json={"version": version, "trusted": True},
    )
    assert response.status_code == 202, response.text
    return ready(client, identifier, headers, "completed")


def download(client, identifier, task, artifact, headers):
    response = client.get(
        f"{API}/{identifier}/tasks/{task['id']}/artifacts/{artifact['path']}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.content) == artifact["size"]
    assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
    return response


def test_analysis_then_sourced_acceptance_reuses_calculation_and_preserves_original_delivery(
    tmp_path,
):
    app, runtime, calls, _ = make_agent_web(
        tmp_path,
        [
            {
                "action": "plan",
                "text": "Confirm synthetic arrays and JSON.",
                "plan": delivery_draft(),
            }
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        response = client.post(
            API,
            headers=headers,
            json={
                "message": "Analyze synthetic arrays [0,2] and [0,0]; deliver JSON, no acceptance.",
            },
        )
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        pending = ready(client, identifier, headers, "waiting_for_user")
        assert calls == []
        assert pending["task"]["baseline"]["requirements"]["criteria"] == []
        first = confirm(client, identifier, headers, 1)
        original = first["task"]
        assert original["schema_version"] == "2.0"
        assert "scenario" not in original
        assert original["baseline"]["resources"] == []
        assert original["result"]["execution_status"] == "completed"
        assert original["result"]["evaluation"] is None
        assert original["result"]["observations"][0]["value"] == pytest.approx(2**0.5)
        assert len(calls) == 1
        original_files = {
            item["path"]: download(client, identifier, original, item, headers).content
            for item in original["artifacts"]
        }
        assert any(path.endswith("/result.json") for path in original_files)
        message = "Require RMSE <= 0.8 m; deliver JSON and Word."
        source = {
            "kind": "user_message",
            "reference": f"message:{identifier}:{len(first['messages'])}",
            "quote": message,
        }
        runtime.planner.model.responses = [
            tool_message(
                "propose_agent",
                {
                    "action": "plan",
                    "text": "Confirm sourced acceptance and delivery.",
                    "plan": delivery_draft(source=source),
                },
            )
        ]
        response = client.post(
            f"{API}/{identifier}/messages",
            headers=headers,
            json={"message": message},
        )
        assert response.status_code == 202, response.text
        pending = ready(client, identifier, headers, "waiting_for_user")
        baseline = pending["task"]["baseline"]
        assert baseline["version"] == 2
        assert baseline["confirmed"] is False
        assert baseline["requirements"]["criteria"][0]["source"] == {
            **source,
            "sha256": None,
            "version": None,
        }
        assert len(calls) == 1
        assert (
            client.post(
                f"{API}/{identifier}/confirm",
                headers=headers,
                json={"version": 1, "trusted": True},
            ).status_code
            == 409
        )
        second = confirm(client, identifier, headers, 2)
        task = second["task"]
        assert task["result"]["execution_status"] == "completed"
        assert task["result"]["evaluation"]["status"] == "fail"
        check = task["result"]["evaluation"]["checks"][0]
        assert check["value"] == pytest.approx(2**0.5)
        assert check["threshold"] == 0.8
        assert check["unit"] == "m"
        assert len(calls) == 1
        assert task["analysis_runs"][0]["reused_from"] == original["analysis_runs"][0]["id"]
        assert task["analysis_runs"][0]["spec"] == original["analysis_runs"][0]["spec"]
        assert (
            task["analysis_runs"][0]["output_sha256"]
            == original["analysis_runs"][0]["output_sha256"]
        )
        archived = second["archived_tasks"][0]
        for key in ("baseline", "analysis_runs", "artifacts", "result"):
            assert archived[key] == original[key]
        for artifact in archived["artifacts"]:
            assert (
                download(client, identifier, archived, artifact, headers).content
                == original_files[artifact["path"]]
            )
        for format in ("json", "docx"):
            artifact = next(item for item in task["artifacts"] if item.get("id") == format)
            result = download(client, identifier, task, artifact, headers)
            if format == "json":
                payload = result.json()
                assert payload["evidence"]["evaluation"]["status"] == "fail"
                assert payload["requirements"]["criteria"][0]["source"]["quote"] == message
            else:
                text = "\n".join(
                    paragraph.text
                    for paragraph in Document(
                        BytesIO(
                            result.content,
                        )
                    ).paragraphs
                )
                assert "FAIL" in text
                assert source["reference"] in text
                assert message in text
        view = client.get(f"{API}/{identifier}/view", headers=headers).json()
        assert view["task"]["evaluation"]["status"] == "fail"
        assert view["archived_tasks"][0]["evaluation"] is None


def test_second_registered_capability_uses_new_schema_metric_unit_and_version_without_routing(
    tmp_path,
):
    app, _, rmse_calls, tools = make_agent_web(
        tmp_path,
        [
            {
                "action": "plan",
                "text": "Confirm synthetic current.",
                "plan": peak_current_draft(),
            }
        ],
    )
    calls = []
    register_peak_current(tools, calls)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        catalog = client.get(f"{API}/capabilities", headers=headers).json()
        assert {"offline_rmse", "sample_peak"} <= {entry["name"] for entry in catalog}
        response = client.post(
            API,
            headers=headers,
            json={
                "message": "Report peak current for synthetic samples [1,-3,2] A, no acceptance.",
            },
        )
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        ready(client, identifier, headers, "waiting_for_user")
        assert calls == rmse_calls == []
        done = confirm(client, identifier, headers, 1)
        assert len(calls) == 1
        assert rmse_calls == []
        assert done["task"]["calls"][0]["tool_version"] == "7"
        view = client.get(f"{API}/{identifier}/view", headers=headers).json()["task"]
        assert view["observations"][0]["metric_id"] == "peak_current"
        assert view["observations"][0]["value"] == 3
        assert view["observations"][0]["unit"] == "A"
        assert view["evaluation"] is None


def test_production_assembly_does_not_install_synthetic_test_plugins(tmp_path):
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        catalog = client.get(f"{API}/capabilities", headers=headers)
        assert catalog.status_code == 200
        names = {entry["name"] for entry in catalog.json()}
        assert {"simulink_step_response", "evaluate_criteria", "deliver_results"} <= names
        assert names.isdisjoint({"offline_rmse", "sample_peak"})
