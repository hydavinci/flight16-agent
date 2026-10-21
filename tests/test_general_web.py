import json

from fastapi.testclient import TestClient

from tests.helpers import tool_message
from tests.test_general_orchestration import register_mass, shell
from tests.test_general_runtime import proposal
from tests.test_verification_web import API, artifact_url, confirm, wait_conversation
from tests.test_web import headers, web_module


def test_workflow_catalog_is_authenticated_and_exposes_registered_strict_schema(project, tmp_path):
    runtime, _ = shell(tmp_path, [])
    register_mass(runtime, [])
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        endpoint = f"{API}/workflows"
        assert client.get(endpoint).status_code == 403
        result = client.get(endpoint, headers=headers(client))
        assert result.status_code == 200, result.text
        catalog = {item["name"]: item for item in result.json()}
        assert catalog["d1"]["directory"] == "worker_project"
        assert catalog["d2"]["directory"] == "diagnostic_data"
        assert catalog["mass_summary"]["directory"] == "none"
        assert catalog["mass_summary"]["version"] == "2.3"
        assert catalog["mass_summary"]["input_schema"]["additionalProperties"] is False


def test_auto_knowledge_api_requires_no_directory_or_backend(project, tmp_path):
    runtime, _ = shell(tmp_path, [tool_message("plan_intent", {
        "action": "answer", "text": "增益裕度描述开环增益到临界稳定的余量。",
    })])
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        response = client.post(API, headers=auth, json={
            "message": "增益裕度是什么？", "scenario": "auto",
        })
        assert response.status_code == 202, response.text
        done = wait_conversation(client, response.json()["id"], auth)
        assert done["status"] == "completed", done
        assert done["project_root"] is None
        assert done["task"] is None
        assert "增益裕度" in done["messages"][-1]["content"]


def test_registered_generic_api_confirms_and_delivers_versioned_outputs(project, tmp_path):
    runtime, _ = shell(tmp_path, [
        tool_message("plan_intent", {
            "action": "plan", "text": "汇总合成质量。",
            "steps": [{"workflow": "mass_summary", "request": "Sum masses.",
                       "inputs": {"components": [2.0, 3.5]}}],
        }),
        tool_message("propose_general", proposal().model_dump(mode="json")),
    ])
    calls = []
    register_mass(runtime, calls)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        response = client.post(API, headers=auth, json={
            "message": "Sum synthetic masses 2 and 3.5 kg.", "scenario": "auto",
        })
        assert response.status_code == 202, response.text
        planned = wait_conversation(client, response.json()["id"], auth)
        assert planned["pending_decision"]["kind"] == "confirm_baseline", planned
        assert not calls
        assert "trim_point" not in planned["task"]["baseline"]
        done = confirm(client, auth, planned)
        assert done["status"] == "completed", done
        assert done["task"]["outputs"] == {"mass": {"total": 5.5, "unit": "kg"}}
        artifact = next(
            item for item in done["task"]["artifacts"] if item["path"] == "execution_record.json"
        )
        endpoint = artifact_url(done, artifact)
        assert client.get(endpoint).status_code == 403
        downloaded = client.get(endpoint, headers=auth)
        assert downloaded.status_code == 200, downloaded.text
        record = json.loads(downloaded.content)
        assert record["calls"][0]["tool_version"] == "2.3"
        assert calls == [[2.0, 3.5]]


def test_api_cannot_resume_previous_task_while_next_stage_needs_data(project, tmp_path):
    next_stage = {"workflow": "d2", "request": "Diagnose using separate recorded data."}
    runtime, _ = shell(tmp_path, [
        tool_message("plan_intent", {
            "action": "plan", "text": "先汇总，再诊断。",
            "steps": [
                {"workflow": "mass_summary", "request": "Sum synthetic masses.",
                 "inputs": {"components": [2.0, 3.5]}},
                next_stage,
            ],
        }),
        tool_message("propose_general", proposal().model_dump(mode="json")),
        tool_message("plan_intent", {
            "action": "plan", "text": "现在需要独立诊断数据。", "steps": [next_stage],
        }),
    ])
    calls = []
    register_mass(runtime, calls)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        response = client.post(API, headers=auth, json={
            "message": "Sum masses, then diagnose the recorded anomaly.", "scenario": "auto",
        })
        assert response.status_code == 202, response.text
        planned = wait_conversation(client, response.json()["id"], auth)
        paused = confirm(client, auth, planned)
        assert paused["status"] == "waiting_for_user", paused
        assert paused["active_workflow"] == "d2"
        assert paused["task"]["scenario"] == "general"
        before = runtime.store.get(paused["id"])
        rejected = client.post(f"{API}/{paused['id']}/resume", headers=auth)
        assert rejected.status_code == 409, rejected.text
        assert runtime.store.get(paused["id"]) == before
        assert calls == [[2.0, 3.5]]
