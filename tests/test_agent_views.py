import pytest
from fastapi.testclient import TestClient

from tests.agent_helpers import draft_data, make_agent_web
from tests.test_agent_web import API, auth, pending, post_message


def views():
    from flight_agent.verification import views as module

    return module


def test_generic_view_preserves_analysis_requirements_and_real_observations(tmp_path):
    _, runtime, _, _ = make_agent_web(tmp_path, [])
    from flight_agent.runtime.agent_contracts import AgentPlan
    from tests.agent_helpers import analysis_plan_data

    planned = runtime.executor.propose(
        runtime.store.create(),
        AgentPlan.model_validate(analysis_plan_data()),
    )
    done = runtime.executor.confirm(planned.id, 1, trusted=True)
    view = views().to_task_view(done.task)
    assert view.schema_version == "2.0"
    assert view.requirements.mode == "analysis"
    assert view.requirements.criteria == []
    assert view.evaluation is None
    assert view.observations[0].value == pytest.approx(2**0.5)
    assert view.observations[0].unit == "m"
    assert view.permissions == ["read_only"]
    assert view.confirmation.confirmed is True
    assert view.baseline_version == 1
    assert view.nodes[0].tool == "offline_rmse"


def test_task_view_rejects_legacy_schema_version(tmp_path):
    from pydantic import ValidationError

    from tests.test_agent_history import agent_task

    data = views().to_task_view(agent_task(tmp_path)).model_dump()
    data["schema_version"] = "1.0"
    with pytest.raises(ValidationError):
        views().TaskView.model_validate(data)


def test_view_endpoint_keeps_raw_record_api_and_requires_authentication(tmp_path):
    app, _, _, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Analyze.", "plan": draft_data()},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers)
        record = pending(client, identifier, headers)
        endpoint = f"{API}/{identifier}/view"
        assert client.get(endpoint).status_code == 403
        response = client.get(endpoint, headers=headers)
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["id"] == identifier
        assert view["task"]["baseline_version"] == 1
        assert view["task"]["confirmation"]["confirmed"] is False
        assert "baseline" not in view["task"]
        raw = client.get(f"{API}/{identifier}", headers=headers).json()
        assert raw["task"]["baseline"] == record["task"]["baseline"]


def test_v2_current_and_archived_views_preserve_calls_without_mutating_record(tmp_path):
    from flight_agent.verification.contracts import Conversation, ToolCall
    from tests.test_agent_history import agent_task

    log = "Historical Worker log.\n" * 500
    task = agent_task(tmp_path)
    task.calls.append(
        ToolCall(
            id="1" * 32,
            node_id="compute",
            tool="offline_rmse",
            baseline_version=1,
            status="completed",
            started_at="now",
            output={"log": log},
        )
    )
    record = Conversation(
        id="b" * 32,
        created_at="now",
        updated_at="now",
        task=task,
        archived_tasks=[task.model_copy(deep=True)],
    )
    before = record.model_dump(mode="json")

    projected = views().to_conversation_view(record)

    assert projected.task.schema_version == "2.0"
    assert projected.task.calls[0].output["log"] == log
    assert projected.archived_tasks[0].schema_version == "2.0"
    assert projected.archived_tasks[0].calls[0].output["log"] == log
    assert record.model_dump(mode="json") == before
