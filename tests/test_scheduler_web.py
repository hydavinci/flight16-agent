from multiprocessing import get_context
from threading import Barrier, Event, Lock, Thread
from typing import cast

import pytest
from fastapi.testclient import TestClient

from flight_agent.config import SchedulerSettings
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling import (
    ExecutionTurnScheduler,
    PlanningScheduler,
    SchedulerClosed,
)
from flight_agent.verification.agent_planner import AgentDecision, AgentPlanner
from flight_agent.verification.coordinator import EngineeringRuntime
from flight_agent.verification.routes import ConversationManager
from flight_agent.web import create_web_app
from tests.agent_helpers import wait_for_conversation
from tests.scheduling_helpers import make_runtime

API = "/api/conversations"


def _assert_runner_drained(runner):
    with pytest.raises(SchedulerClosed):
        with runner.reserve():
            pytest.fail("closed scheduler admitted work")
    assert runner._closed
    assert all(not thread.is_alive() for thread in runner._pool._threads)


def _planning_worker_close_with_external(completed):
    planning = PlanningScheduler(1, 0)
    execution = ExecutionTurnScheduler(1)
    manager = ConversationManager(
        cast(EngineeringRuntime, object()), planning, execution
    )
    entered, invoke_close, worker_returned = Event(), Event(), Event()
    external_done = Event()

    def operation():
        entered.set()
        assert invoke_close.wait(5)
        manager.close()
        worker_returned.set()

    def close_external():
        manager.close()
        external_done.set()

    with planning.reserve() as reservation:
        future = reservation.submit(operation)
    assert entered.wait(5)
    closer = Thread(target=close_external, daemon=True)
    closer.start()
    with planning._condition:
        assert planning._condition.wait_for(
            lambda: planning._shutdown_started, timeout=5
        )
    invoke_close.set()
    assert worker_returned.wait(5)
    future.result(timeout=5)
    assert external_done.wait(5)
    closer.join(5)
    assert not closer.is_alive()
    _assert_runner_drained(planning)
    _assert_runner_drained(execution)
    completed.set()


def _assert_process_completes(target, message):
    context = get_context("spawn")
    completed = context.Event()
    process = context.Process(target=target, args=(completed,))
    process.start()
    try:
        assert completed.wait(5), message
        process.join(5)
        assert not process.is_alive(), "scheduler child process did not exit"
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


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


def test_planning_worker_close_yields_to_external_drain_without_deadlock():
    _assert_process_completes(
        _planning_worker_close_with_external,
        "planning worker close deadlocked with the external drain owner",
    )


def test_manager_close_orders_lifecycle_and_is_terminally_idempotent():
    events = []

    class OrderedPlanningScheduler(PlanningScheduler):
        def stop_accepting(self):
            events.append("planning:stop")
            super().stop_accepting()

        def close(self):
            events.append("planning:close")
            super().close()

    class OrderedExecutionScheduler(ExecutionTurnScheduler):
        def stop_accepting(self):
            events.append("execution:stop")
            super().stop_accepting()

        def close(self):
            events.append("execution:close")
            super().close()

    planning = OrderedPlanningScheduler(1, 0)
    execution = OrderedExecutionScheduler(1)
    manager = ConversationManager(
        cast(EngineeringRuntime, object()), planning, execution
    )
    manager.close()
    manager.close()
    assert events == [
        "planning:stop",
        "execution:stop",
        "planning:close",
        "planning:stop",
        "execution:close",
        "execution:stop",
    ]
    _assert_runner_drained(planning)
    _assert_runner_drained(execution)


def test_execution_completion_callback_close_requires_external_drain():
    planning = PlanningScheduler(1, 0)
    execution = ExecutionTurnScheduler(1)
    manager = ConversationManager(
        cast(EngineeringRuntime, object()), planning, execution
    )
    entered, release, callback_returned = Event(), Event(), Event()

    def operation():
        entered.set()
        assert release.wait(5)

    def close_from_callback(_future):
        manager.close()
        callback_returned.set()

    try:
        with execution.reserve() as reservation:
            future = reservation.submit(operation)
        assert entered.wait(5)
        future.add_done_callback(close_from_callback)
        release.set()
        future.result(timeout=5)
        assert callback_returned.wait(5)
        with pytest.raises(SchedulerClosed):
            with planning.reserve():
                pytest.fail("planning admitted work after callback close")
        with pytest.raises(SchedulerClosed):
            with execution.reserve():
                pytest.fail("execution admitted work after callback close")
        manager.close()
        _assert_runner_drained(planning)
        _assert_runner_drained(execution)
    finally:
        release.set()
        planning.close()
        execution.close()


def test_concurrent_external_close_has_one_drain_owner_and_waiters():
    counter_lock = Lock()

    class CountingPlanningScheduler(PlanningScheduler):
        def __init__(self):
            super().__init__(1, 0)
            self.close_calls = 0
            self.close_started = Event()

        def close(self):
            with counter_lock:
                self.close_calls += 1
            self.close_started.set()
            super().close()

    class CountingExecutionScheduler(ExecutionTurnScheduler):
        def __init__(self):
            super().__init__(1)
            self.close_calls = 0

        def close(self):
            with counter_lock:
                self.close_calls += 1
            super().close()

    planning = CountingPlanningScheduler()
    execution = CountingExecutionScheduler()
    manager = ConversationManager(
        cast(EngineeringRuntime, object()), planning, execution
    )
    entered, release = Event(), Event()
    barrier = Barrier(3)
    done = (Event(), Event())

    def operation():
        entered.set()
        assert release.wait(5)

    def close_external(finished):
        barrier.wait()
        manager.close()
        finished.set()

    try:
        with planning.reserve() as reservation:
            future = reservation.submit(operation)
        assert entered.wait(5)
        threads = [
            Thread(target=close_external, args=(finished,), daemon=True)
            for finished in done
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        assert planning.close_started.wait(5)
        release.set()
        future.result(timeout=5)
        for finished in done:
            assert finished.wait(5)
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert planning.close_calls == 1
        assert execution.close_calls == 1
        _assert_runner_drained(planning)
        _assert_runner_drained(execution)
        manager.close()
        assert planning.close_calls == 1
        assert execution.close_calls == 1
    finally:
        release.set()
        planning.close()
        execution.close()


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


def test_queued_view_exposes_action_without_claiming_execution(tmp_path):
    entered, release = Event(), Event()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            client.post(API, headers=headers, json={"message": "Blocker"})
            assert entered.wait(5)
            queued = client.post(API, headers=headers, json={"message": "Queued"}).json()
            view = client.get(f"{API}/{queued['id']}/view", headers=headers).json()
            assert view["status"] == "queued"
            assert view["queued_action"] == "message"
            assert view["task"] is None
        finally:
            release.set()


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


def test_request_cannot_supply_a_lane_to_bypass_server_metadata(tmp_path):
    entered, release = Event(), Event()
    release.set()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        response = client.post(
            API, headers=headers, json={"message": "Run this", "lane": "read_only"},
        )
        assert response.status_code == 422
        assert runtime.store.recent() == []


def test_planning_progresses_while_another_conversation_executes_local_matlab(tmp_path):
    from tests.scheduling_helpers import propose_fake

    entered, release = Event(), Event()

    class Planner(AgentPlanner):
        def propose(self, message, conversation, **kwargs):
            return AgentDecision(action="answer", text="Independent planning completed.")

    def local_operation(context, inputs):
        entered.set()
        assert release.wait(5)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, Planner(), local_operation, lane="local_matlab")
    pending = propose_fake(runtime, lane="local_matlab")
    app = create_web_app(tmp_path, engineering_runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            response = client.post(
                f"{API}/{pending.id}/confirm", headers=headers,
                json={"version": 1, "trusted": True},
            )
            assert response.status_code == 202
            assert entered.wait(5)
            question = client.post(API, headers=headers, json={"message": "Explain RMSE."})
            assert question.status_code == 202
            wait_for_conversation(
                client, question.json()["id"], headers,
                lambda record: record["status"] == "completed",
            )
        finally:
            release.set()
