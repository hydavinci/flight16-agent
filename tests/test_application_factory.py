from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from flight_agent.runtime.scheduling import (
    ExecutionTurnScheduler,
    PlanningScheduler,
    SchedulerClosed,
)
from flight_agent.verification.routes import ConversationManager
from flight_agent.web import create_web_app
from tests.agent_helpers import make_agent_web, wait_for_conversation


def test_injected_runtime_manager_and_schedulers_keep_identity(tmp_path):
    fixture_app, runtime, calls, _ = make_agent_web(
        tmp_path / "fixture",
        [{"action": "answer", "text": "No execution."}],
    )
    fixture_app.state.conversation_manager.close()
    settings = runtime.executor.scheduler.settings
    planning = PlanningScheduler(settings.planning_workers, settings.planning_queue)
    execution = ExecutionTurnScheduler(settings.execution_turns)
    manager = ConversationManager(runtime, planning, execution)
    app = create_web_app(
        tmp_path / "app",
        engineering_runtime=runtime,
        conversation_manager=manager,
    )
    try:
        assert app.state.services.runtime is runtime
        assert app.state.services.resources is runtime.resources
        assert app.state.services.local_backend.snapshots is runtime.executor.snapshots
        assert app.state.services.manager is manager
        assert app.state.conversation_manager is manager
        assert manager.planning is planning
        assert manager.execution is execution
        with TestClient(app, base_url="http://127.0.0.1") as client:
            headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
            response = client.post(
                "/api/conversations",
                headers=headers,
                json={"message": "Explain RMSE."},
            )
            assert response.status_code == 202
            done = wait_for_conversation(
                client,
                response.json()["id"],
                headers,
                lambda item: item["status"] == "completed",
            )
            assert done["messages"][-1]["content"] == "No execution."
        assert app.state.services.owns_manager is False
        with planning.reserve():
            pass
        assert calls == []
        assert not (tmp_path / "app" / "runs").exists()
    finally:
        manager.close()


def test_default_application_closes_manager_but_store_remains_usable(tmp_path):
    app = create_web_app(tmp_path)
    services = app.state.services
    saved = services.runtime.store.create()

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/bootstrap").status_code == 200

    assert services.owns_manager is True
    with pytest.raises(SchedulerClosed):
        with services.manager.planning.reserve():
            pass
    assert services.runtime.store.get(saved.id).id == saved.id


def test_default_application_records_the_bound_local_backend(tmp_path):
    app = create_web_app(tmp_path)
    services = app.state.services
    try:
        assert services.runtime.local_matlab_backend is services.local_backend
        assert services.local_backend.snapshots is services.runtime.executor.snapshots
    finally:
        services.close()


def test_manager_cannot_be_bound_to_a_different_runtime_before_composition(
    tmp_path, monkeypatch
):
    import flight_agent.application as application

    first_app, first, _, _ = make_agent_web(tmp_path / "first", [])
    second_app, second, _, _ = make_agent_web(tmp_path / "second", [])

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid injection must fail before application composition")

    monkeypatch.setattr(application.LocalMatlabBackend, "from_environment", forbidden)
    monkeypatch.setattr(application.RemoteSimulinkBackend, "from_environment", forbidden)
    monkeypatch.setattr(application, "SQLiteConversationStore", forbidden)
    try:
        with pytest.raises(ValueError, match="manager and runtime"):
            create_web_app(
                tmp_path / "app",
                engineering_runtime=second,
                conversation_manager=first_app.state.conversation_manager,
            )
        assert first is not second
    finally:
        first_app.state.conversation_manager.close()
        second_app.state.conversation_manager.close()


def test_injected_scheduler_settings_must_match_runtime_before_composition(
    tmp_path, monkeypatch
):
    import flight_agent.application as application

    fixture_app, runtime, _, _ = make_agent_web(tmp_path / "fixture", [])
    settings = runtime.executor.scheduler.settings
    different = replace(
        settings,
        planning_workers=1 if settings.planning_workers != 1 else 2,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid injection must fail before application composition")

    monkeypatch.setattr(application.LocalMatlabBackend, "from_environment", forbidden)
    monkeypatch.setattr(application.RemoteSimulinkBackend, "from_environment", forbidden)
    monkeypatch.setattr(application, "SQLiteConversationStore", forbidden)
    try:
        with pytest.raises(ValueError, match="scheduler settings"):
            create_web_app(
                tmp_path / "app",
                engineering_runtime=runtime,
                scheduler_settings=different,
            )
    finally:
        fixture_app.state.conversation_manager.close()
