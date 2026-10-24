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


def test_legacy_verification_projection_does_not_invent_requirement_sources(tmp_path):
    from flight_agent.verification.compat import to_legacy_view
    from tests.test_verification_runtime import confirm, runtime_fixture

    runtime, _, _, root = runtime_fixture(tmp_path)
    done = confirm(runtime, runtime.submit(None, "Verify the supplied project.", str(root)))
    original = done.model_dump(mode="json")
    view = to_legacy_view(done.task)
    assert view.schema_version == "1.0"
    assert view.requirements.mode == "verification"
    assert all(item.source is None for item in view.requirements.criteria)
    assert all("历史记录未提供来源" in item.source_note for item in view.requirements.criteria)
    assert view.evaluation.status == done.task.evaluation.status
    assert [item.status for item in view.evaluation.checks] == [
        item.status for item in done.task.evaluation.checks
    ]
    assert done.model_dump(mode="json") == original


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


def test_legacy_recovery_projection_preserves_both_adoption_and_original_continuation(
    tmp_path,
    monkeypatch,
):
    from flight_agent.verification.compat import to_legacy_view
    from tests.test_verification_replanning import failed_delivery
    from tests.test_verification_runtime import proposal

    _, _, _, _, failed, _ = failed_delivery(tmp_path, monkeypatch, proposal(threshold=4))
    view = to_legacy_view(failed.task)
    assert view.confirmation.recovery_action == "resume"
    assert view.confirmation.recovery_proposal["action"] == "plan"


def test_historical_worker_log_does_not_block_current_or_archived_view(tmp_path):
    from flight_agent.verification.compat import to_legacy_view
    from tests.test_verification_runtime import confirm, runtime_fixture

    runtime, _, _, root = runtime_fixture(tmp_path)
    done = confirm(runtime, runtime.submit(None, "Verify historical results.", str(root)))
    log = "Historical Worker log.\n" * 500
    done.task.worker_run = done.task.worker_run.model_copy(update={"log": log})
    runtime.store.save(done)
    assert to_legacy_view(done.task).details["historical_record"]["worker_run"]["log"] == log
    from flight_agent.verification.views import to_conversation_view

    done.archived_tasks.append(done.task.model_copy(deep=True))
    projected = to_conversation_view(done)
    assert projected.task.evaluation.status == "pass"
    assert projected.archived_tasks[0].details["historical_record"]["worker_run"]["log"] == log


def test_invalid_historical_analysis_cannot_be_projected_as_valid_metrics(tmp_path):
    from flight_agent.verification.compat import to_legacy_view
    from tests.test_verification_runtime import confirm, runtime_fixture

    runtime, _, _, root = runtime_fixture(tmp_path)
    done = confirm(runtime, runtime.submit(None, "Verify historical results.", str(root)))
    analysis = done.task.result.analyses[0]
    invalid = analysis.model_copy(
        update={
            "valid": False,
            "diagnostics": ["Steady-state response is not stationary."],
        }
    )
    done.task.result = done.task.result.model_copy(update={"analyses": [invalid]})
    projected = to_legacy_view(done.task)
    assert projected.observations
    assert all(not value.valid for value in projected.observations)
    assert all("not stationary" in value.reason for value in projected.observations)
    assert projected.evaluation.status == done.task.evaluation.status
