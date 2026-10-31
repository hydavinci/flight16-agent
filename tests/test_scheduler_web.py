from threading import Event
from typing import cast

from fastapi.testclient import TestClient

from flight_agent.config import SchedulerSettings
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling import ExecutionTurnScheduler, PlanningScheduler
from flight_agent.verification.agent_planner import AgentDecision, AgentPlanner
from flight_agent.verification.coordinator import EngineeringRuntime
from flight_agent.verification.routes import ConversationManager
from flight_agent.web import create_web_app
from tests.agent_helpers import wait_for_conversation
from tests.scheduling_helpers import make_runtime

API = "/api/conversations"


def build(tmp_path, entered, release):
    class Planner(AgentPlanner):
        def propose(self, message, conversation, **kwargs):
            entered.set()
            assert release.wait(5)
            return AgentDecision(action="answer", text="Answered without execution.")

    settings = SchedulerSettings(planning_workers=1, planning_queue=1)
    runtime = make_runtime(
        tmp_path,
        Planner(),
        lambda context, inputs: CapabilityOutcome(),
        settings=settings,
    )
    return create_web_app(
        tmp_path,
        engineering_runtime=runtime,
        scheduler_settings=settings,
    ), runtime


def test_manager_close_stops_both_admissions_before_draining_exactly_once():
    events = []

    class Scheduler:
        def __init__(self, name):
            self.name = name

        def stop_accepting(self):
            events.append(f"{self.name}:stop")

        def close(self):
            events.append(f"{self.name}:close")

    manager = ConversationManager(
        cast(EngineeringRuntime, object()),
        cast(PlanningScheduler, Scheduler("planning")),
        cast(ExecutionTurnScheduler, Scheduler("execution")),
    )
    manager.close()
    manager.close()
    assert events == [
        "planning:stop",
        "execution:stop",
        "planning:close",
        "execution:close",
    ]


def test_saturation_is_queued_then_typed_capacity_without_phantom_messages(tmp_path):
    entered, release = Event(), Event()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            a = client.post(API, headers=headers, json={"message": "First"})
            assert a.status_code == 202
            assert a.json()["status"] == "queued"
            assert entered.wait(5)
            b = client.post(API, headers=headers, json={"message": "Second"})
            assert b.status_code == 202
            identifier = b.json()["id"]
            queued = client.get(f"{API}/{identifier}", headers=headers).json()
            assert queued["status"] == "queued"
            assert queued["queued_action"] == "message"
            c = client.post(API, headers=headers, json={"message": "Rejected"})
            assert c.status_code == 429
            assert c.json()["detail"]["code"] == "scheduler_capacity"
            assert c.headers["retry-after"] == "1"
            assert len(runtime.store.recent()) == 2
        finally:
            release.set()
        wait_for_conversation(
            client,
            identifier,
            headers,
            lambda record: record["status"] == "completed",
        )


def test_same_queued_conversation_conflict_does_not_append_another_turn(tmp_path):
    entered, release = Event(), Event()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            assert (
                client.post(
                    API, headers=headers, json={"message": "Blocker"}
                ).status_code
                == 202
            )
            assert entered.wait(5)
            b = client.post(
                API, headers=headers, json={"message": "Queued"}
            ).json()
            before = runtime.store.get(b["id"]).model_dump()
            response = client.post(
                f"{API}/{b['id']}/messages",
                headers=headers,
                json={"message": "Must not append"},
            )
            assert response.status_code == 409
            assert runtime.store.get(b["id"]).model_dump() == before
        finally:
            release.set()


def test_closed_manager_returns_503_without_creating_a_conversation(tmp_path):
    entered, release = Event(), Event()
    release.set()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        app.state.conversation_manager.close()
        result = client.post(
            API, headers=headers, json={"message": "Not accepted"}
        )
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "scheduler_closed"
        assert runtime.store.recent() == []


def test_failed_pool_publication_is_visible_and_reservation_can_be_reused(
    tmp_path, monkeypatch
):
    from flight_agent.runtime.scheduling import Submission

    entered, release = Event(), Event()
    release.set()
    app, runtime = build(tmp_path, entered, release)

    def unavailable(self, operation):
        raise RuntimeError("Pool publication failed")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        monkeypatch.setattr(Submission, "submit", unavailable)
        for index in range(3):
            response = client.post(
                API,
                headers=headers,
                json={"message": f"Question {index}"},
            )
            assert response.status_code == 503
        records = runtime.store.recent()
        assert len(records) == 3
        assert all(record.status == "failed" for record in records)
        assert all(record.queued_action is None for record in records)
        assert all(record.error == "Pool publication failed" for record in records)
