import json
import logging
import sqlite3
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from flight_agent.copilot_diagnostics import CopilotError, make_diagnostics
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.planning_contracts import PlanningMetrics, PlanningTelemetry
from flight_agent.runtime.scheduling import ExecutionTurnScheduler, PlanningScheduler
from flight_agent.verification.agent_planner import AgentDecision
from flight_agent.verification.contracts import PendingDecision
from flight_agent.verification.routes import ConversationManager, MessageRequest
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from tests.agent_helpers import draft_data, make_agent_web, wait_for_conversation
from tests.scheduling_helpers import make_runtime, propose_fake


def _telemetry(*, failure=None, serialized_bytes=100):
    return PlanningTelemetry(
        metrics=PlanningMetrics(
            serialized_bytes=serialized_bytes,
            selected_capability_count=2,
            selected_source_excerpt_count=1,
            planning_duration_seconds=0.2,
            correction_round_count=0,
        ),
        failure=failure,
    )


def _manager(runtime):
    return ConversationManager(
        runtime,
        PlanningScheduler(1, 0),
        ExecutionTurnScheduler(1),
    )


def test_managed_success_persists_metrics_and_clears_them_on_next_admission(tmp_path):
    telemetry = _telemetry()

    class Planner:
        def propose(self, message, conversation, *, observe=None, **kwargs):
            assert observe is not None
            observe(telemetry)
            return AgentDecision(action="answer", text="Safe answer.")

    runtime = make_runtime(tmp_path, Planner(), lambda context, inputs: CapabilityOutcome())
    manager = _manager(runtime)
    try:
        queued = manager.submit(None, MessageRequest(message="Question"))
        manager.close()
        saved = runtime.store.get(queued.id)
        assert saved.status == "completed"
        assert saved.planning_telemetry == telemetry

        queued_again = runtime.queue_message(saved.id, "Another question")
        assert queued_again.status == "queued"
        assert queued_again.planning_telemetry is None
        assert runtime.store.get(saved.id).planning_telemetry is None
    finally:
        manager.close()


def test_managed_plan_success_exposes_telemetry_and_survives_restart(tmp_path):
    app, runtime, _, _ = make_agent_web(
        tmp_path,
        [{"action": "plan", "text": "Confirm analysis.", "plan": draft_data()}],
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        queued = client.post(
            "/api/conversations",
            headers=headers,
            json={"message": "Analyze supplied arrays."},
        ).json()
        response = wait_for_conversation(
            client,
            queued["id"],
            headers,
            lambda record: record["status"] == "waiting_for_user",
        )

    assert response["task"] is not None
    assert response["planning_telemetry"]["metrics"]["serialized_bytes"] > 0
    assert response["planning_telemetry"]["failure"] is None
    restarted = SQLiteConversationStore(runtime.store.root)
    restarted_telemetry = restarted.get(queued["id"]).planning_telemetry
    assert restarted_telemetry is not None
    assert restarted_telemetry.model_dump(mode="json") == response["planning_telemetry"]


def test_sqlite_restart_and_export_preserve_allowlisted_telemetry(tmp_path):
    root = tmp_path / "state"
    store = SQLiteConversationStore(root, require_empty=True)
    record = store.create()
    record.planning_telemetry = _telemetry()
    store.save(record)

    restarted = SQLiteConversationStore(root)
    saved = restarted.get(record.id)
    exported = json.loads(restarted.export(record.id))

    assert saved.planning_telemetry == record.planning_telemetry
    assert exported["conversation"]["planning_telemetry"] == record.planning_telemetry.model_dump(
        mode="json"
    )


def test_sqlite_row_without_optional_telemetry_decodes_as_none(tmp_path):
    root = tmp_path / "state"
    store = SQLiteConversationStore(root, require_empty=True)
    record = store.create()
    with sqlite3.connect(store.database_path) as connection:
        state = json.loads(
            connection.execute(
                "SELECT state_json FROM conversations WHERE id=?", (record.id,)
            ).fetchone()[0]
        )
        state.pop("planning_telemetry")
        connection.execute(
            "UPDATE conversations SET state_json=? WHERE id=?",
            (json.dumps(state), record.id),
        )

    restarted = SQLiteConversationStore(root)
    assert restarted.get(record.id).planning_telemetry is None


@pytest.mark.parametrize(
    "category",
    [
        "authentication",
        "authorization",
        "transport",
        "timeout",
        "invalid_response",
        "process_failure",
    ],
)
def test_managed_provider_failure_persists_only_safe_diagnostics_and_logs(
    tmp_path, caplog, category
):
    diagnostic = make_diagnostics(
        category,
        stdout="private-response",
        stderr=(
            "WebSocket closed\nrequest-id: private-request-id\n"
            "Authorization: private-token\n/private/project"
        ),
        exit_code=7,
        elapsed_seconds=0.2,
    )
    observed_telemetry = _telemetry()
    expected_telemetry = _telemetry(failure=diagnostic)

    class Planner:
        def propose(self, message, conversation, *, observe=None, **kwargs):
            assert observe is not None
            observe(observed_telemetry)
            try:
                raise ValueError("private chained prompt and stderr")
            except ValueError:
                raise CopilotError(diagnostic) from None

    calls = []
    runtime = make_runtime(
        tmp_path,
        Planner(),
        lambda context, inputs: calls.append(context.call_id) or CapabilityOutcome(),
    )
    pending = propose_fake(runtime)
    original_task = pending.task.model_dump(mode="json")
    original_archived = [task.model_dump(mode="json") for task in pending.archived_tasks]
    manager = _manager(runtime)
    try:
        with caplog.at_level(logging.ERROR):
            queued = manager.submit(
                pending.id,
                MessageRequest(message="Explain without executing."),
            )
            manager.close()
    finally:
        manager.close()

    saved = runtime.store.get(queued.id)
    expected_error = str(CopilotError(diagnostic))
    assert saved.status == "failed"
    assert saved.pending_decision == PendingDecision(kind="clarify", message=expected_error)
    assert saved.error == expected_error
    assert saved.planning_telemetry == expected_telemetry
    assert saved.task.model_dump(mode="json") == original_task
    assert [task.model_dump(mode="json") for task in saved.archived_tasks] == original_archived
    assert calls == []

    durable = json.dumps(saved.model_dump(mode="json"))
    for secret in (
        "private-response",
        "private-token",
        "private-request-id",
        "/private/project",
        "private chained prompt",
    ):
        assert secret not in durable
        assert secret not in caplog.text
    provider_logs = [
        record for record in caplog.records if "Copilot planning failed" in record.message
    ]
    assert provider_logs
    assert all(not record.exc_info for record in provider_logs)
    assert all(f"category={category}" in record.message for record in provider_logs)
    assert all("exit_code=7" in record.message for record in provider_logs)


def test_blocked_managed_planner_releases_lock_and_cannot_overwrite_superseding_state(
    tmp_path,
):
    planner_entered = Event()
    release_planner = Event()
    superseding_state_saved = Event()
    stale = _telemetry(serialized_bytes=111)
    newer = _telemetry(serialized_bytes=222)

    class Planner:
        def propose(self, message, conversation, *, observe=None, **kwargs):
            assert observe is not None
            observe(stale)
            planner_entered.set()
            release_planner.wait()
            return AgentDecision(action="answer", text="Stale answer.")

    runtime = make_runtime(tmp_path, Planner(), lambda context, inputs: CapabilityOutcome())
    manager = _manager(runtime)
    publisher = None
    try:
        queued = manager.submit(None, MessageRequest(message="Question"))
        assert planner_entered.wait(1)

        def publish_superseding_state():
            with runtime.locks.hold(queued.id):
                current = runtime.store.get(queued.id)
                current.status = "waiting_for_user"
                current.queued_action = None
                current.queue_origin = None
                current.pending_decision = PendingDecision(
                    kind="clarify", message="Newer clarification"
                )
                current.planning_telemetry = newer
                runtime.store.save(current)
            superseding_state_saved.set()

        publisher = Thread(target=publish_superseding_state)
        publisher.start()
        assert superseding_state_saved.wait(1), (
            "managed planning held the conversation lock across planner inference"
        )
        release_planner.set()
        manager.close()
    finally:
        release_planner.set()
        manager.close()
        if publisher is not None:
            publisher.join()

    saved = runtime.store.get(queued.id)
    assert saved.status == "waiting_for_user"
    assert saved.pending_decision.message == "Newer clarification"
    assert saved.planning_telemetry == newer
    assert all(message.content != "Stale answer." for message in saved.messages)


def test_existing_conversation_endpoint_exposes_validated_telemetry(tmp_path):
    app, runtime, _, _ = make_agent_web(tmp_path, [])
    record = runtime.store.create()
    record.planning_telemetry = _telemetry()
    runtime.store.save(record)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        response = client.get(
            f"/api/conversations/{record.id}",
            headers={"x-flight-token": token},
        )

    assert response.status_code == 200
    assert response.json()["planning_telemetry"] == record.planning_telemetry.model_dump(
        mode="json"
    )
