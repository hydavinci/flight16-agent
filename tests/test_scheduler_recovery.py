from pathlib import Path

import pytest

from flight_agent.runtime.evidence import (
    agent_receipt_name,
    describe_evidence,
    publish_evidence,
)
from flight_agent.runtime.observations import (
    CapabilityOutcome,
    Observation,
    OutputArtifact,
)
from flight_agent.runtime.scheduling import ExecutionTurnScheduler, PlanningScheduler
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.contracts import ConversationState
from flight_agent.verification.routes import (
    ConfirmationRequest,
    ConversationManager,
)
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import analysis_plan_data
from tests.scheduling_helpers import make_runtime, propose_fake


def reopen(runtime):
    store = ConversationStore(runtime.store.root)
    executor = AgentExecutor(
        store,
        runtime.executor.tools,
        runtime.resources,
        scheduler=runtime.executor.scheduler,
    )
    return store, executor


def evidence_operation(calls, terminal):
    def operation(context, inputs):
        calls.append(context.call_id)
        context.save_checkpoint({"node": context.node_id, "terminal": True})
        if context.node_id == "next":
            if terminal == "interrupt":
                raise KeyboardInterrupt("Crash after dispatch commit")
            if terminal == "fail":
                raise RuntimeError("Known invocation failure")
        path = f"data/{context.node_id}.json"
        publish_evidence(context.directory, path, b'{"value":1}')
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id,
                    metric_id="rmse",
                    label="RMSE",
                    value=1.0,
                    unit="m",
                )
            ],
            artifacts=[
                OutputArtifact(
                    **describe_evidence(context.directory, path).model_dump(),
                    id=f"raw-{context.node_id}",
                    node_id=context.node_id,
                )
            ],
        )

    return operation


def two_node_plan():
    data = analysis_plan_data()
    data["nodes"].append(
        {
            **data["nodes"][0],
            "id": "next",
            "depends_on": ["compute"],
        }
    )
    return data


def preserved_task_state(record):
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    return [
        {
            "baseline": task.baseline,
            "nodes": task.nodes,
            "calls": task.calls,
            "decisions": task.decisions,
            "analysis_runs": task.analysis_runs,
            "pending_specs": task.pending_specs,
            "checkpoints": task.checkpoints,
            "checkpoint_sha256": task.checkpoint_sha256,
            "artifacts": task.artifacts,
            "budget_state": task.budget_state,
        }
        for task in tasks
    ]


def receipt_bytes(record):
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    return {
        run.id: (
            Path(task.result_directory) / agent_receipt_name(run.id)
        ).read_bytes()
        for task in tasks
        for run in task.analysis_runs
    }


def queue_committed_action(tmp_path, action):
    calls = []
    terminal = None if action == "confirm" else "fail"
    runtime = make_runtime(
        tmp_path,
        None,
        evidence_operation(calls, terminal),
    )
    pending = propose_fake(
        runtime,
        data=two_node_plan() if action != "confirm" else analysis_plan_data(),
    )
    if action == "confirm":
        completed = runtime.executor.confirm(pending.id, 1, trusted=True)
        assert completed.status == "completed"
        pending = propose_fake(runtime)
    else:
        failed = runtime.executor.confirm(pending.id, 1, trusted=True)
        assert failed.status == "failed"
        pending = failed
    calls.clear()
    kwargs = (
        {"version": pending.task.baseline.version, "trusted": True}
        if action in ("confirm", "retry")
        else {"version": None, "trusted": False}
    )
    queued, task_id = runtime._queue_decision(
        pending.id,
        action,
        **kwargs,
    )
    assert task_id == queued.task.id
    assert queued.status == "queued"
    assert queued.queued_action == action
    assert queued.model_dump().get("queued_action_committed") is True
    return runtime, queued, calls


def test_queued_message_restart_does_not_call_a_planner_or_lose_the_message(tmp_path):
    runtime = make_runtime(tmp_path, None, lambda context, inputs: CapabilityOutcome())
    queued = runtime.queue_message(None, "Explain the saved measurement.")
    store, executor = reopen(runtime)
    restored = store.get(queued.id)
    assert restored.status == "interrupted"
    assert restored.pending_decision.kind == "clarify"
    assert restored.messages[0].content == "Explain the saved measurement."
    assert restored.queued_action is None
    assert restored.task is None


def test_queued_followup_restores_original_pending_confirmation(tmp_path):
    runtime = make_runtime(tmp_path, None, lambda context, inputs: CapabilityOutcome())
    original = propose_fake(runtime)
    runtime.queue_message(original.id, "Explain this plan before I confirm.")
    store, executor = reopen(runtime)
    restored = store.get(original.id)
    assert restored.status == original.status
    assert restored.pending_decision == original.pending_decision
    assert restored.task == original.task
    assert restored.turn_origin is None
    assert restored.messages[-1].role == "assistant"


def test_queued_confirmation_restart_never_replays_consent(tmp_path):
    calls = []
    runtime = make_runtime(
        tmp_path,
        None,
        lambda context, inputs: (
            calls.append(context.call_id),
            CapabilityOutcome(),
        )[1],
    )
    pending = propose_fake(runtime)
    pending.queue_origin = ConversationState(
        status=pending.status,
        pending_decision=pending.pending_decision,
        error=pending.error,
    )
    pending.queued_action = "confirm"
    pending.status = "queued"
    runtime.store.save(pending)
    store, executor = reopen(runtime)
    restored = store.get(pending.id)
    assert restored.status == "waiting_for_user"
    assert restored.pending_decision.kind == "confirm_baseline"
    assert not restored.task.baseline.confirmed
    assert not restored.task.calls
    assert calls == []


def test_crash_waiting_for_local_slot_is_not_an_unknown_external_call(
    tmp_path, monkeypatch
):
    calls = []
    runtime = make_runtime(
        tmp_path,
        None,
        lambda context, inputs: (
            calls.append(context.call_id),
            CapabilityOutcome(),
        )[1],
        lane="local_matlab",
    )
    pending = propose_fake(runtime, lane="local_matlab")
    save = runtime.store.save

    def crash_after_queue(record):
        save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            raise KeyboardInterrupt("Process stopped before acquiring a lane")

    monkeypatch.setattr(runtime.store, "save", crash_after_queue)
    with pytest.raises(KeyboardInterrupt):
        runtime.executor.confirm(pending.id, 1, trusted=True)
    store, executor = reopen(runtime)
    restored = store.get(pending.id)
    assert restored.status == "interrupted"
    assert restored.pending_decision.kind == "resume"
    assert restored.task.nodes[0].status == "pending"
    assert restored.task.calls == []
    assert restored.task.budget_state.calls == 0
    assert calls == []
    done = executor.resume(pending.id)
    assert done.status == "completed"
    assert len(calls) == 1


def test_unknown_dispatched_call_keeps_original_identity_and_blocks_retry(tmp_path):
    calls = []

    def uncertain(context, inputs):
        calls.append(context.call_id)
        raise KeyboardInterrupt("Process stopped after dispatched-call commit")

    runtime = make_runtime(tmp_path, None, uncertain, lane="local_matlab")
    pending = propose_fake(runtime, lane="local_matlab")
    with pytest.raises(KeyboardInterrupt):
        runtime.executor.confirm(pending.id, 1, trusted=True)
    original = runtime.store.get(pending.id).task.calls[0].id
    store, executor = reopen(runtime)
    restored = store.get(pending.id)
    assert restored.task.calls[0].status == "interrupted"
    assert restored.task.calls[0].id == original
    assert restored.task.budget_state.calls == 1
    assert original in restored.task.pending_specs
    blocked = executor.resume(pending.id)
    assert blocked.status == "interrupted"
    assert calls == [original]
    with pytest.raises(ValueError, match="unknown|unresolved|reconcile"):
        executor.retry(pending.id, 1, trusted=True)


def test_recovery_is_not_limited_to_recent_twenty_conversations(tmp_path):
    runtime = make_runtime(tmp_path, None, lambda context, inputs: CapabilityOutcome())
    identifiers = [
        runtime.queue_message(None, f"Question {index}").id for index in range(25)
    ]
    store, executor = reopen(runtime)
    assert all(
        store.get(identifier).status == "interrupted" for identifier in identifiers
    )


@pytest.mark.parametrize("action", ["confirm", "resume", "retry"])
def test_committed_action_crash_recovers_without_rolling_back_task_state(
    tmp_path, action
):
    runtime, queued, calls = queue_committed_action(tmp_path, action)
    task_state = preserved_task_state(queued)
    receipts = receipt_bytes(queued)
    messages = len(queued.messages)

    first_store, executor = reopen(runtime)
    first = first_store.get(queued.id)

    assert first.status == "interrupted"
    expected = (
        "retry"
        if any(node.status == "failed" for node in first.task.nodes)
        else "resume"
    )
    assert first.pending_decision.kind == expected
    assert first.queued_action is None
    assert first.queue_origin is None
    assert first.model_dump().get("queued_action_committed") is False
    assert first.task.baseline.confirmed
    assert preserved_task_state(first) == task_state
    assert receipt_bytes(first) == receipts
    assert len(first.messages) == messages + 1
    assert calls == []

    second_store = ConversationStore(first_store.root)
    second = second_store.get(queued.id)

    assert second == first
    assert preserved_task_state(second) == task_state
    assert receipt_bytes(second) == receipts
    assert calls == []
    if action == "resume":
        retried = executor.retry(
            second.id,
            second.task.baseline.version,
            trusted=True,
        )
        assert retried.status == "failed"
        assert len(calls) == 1
        assert retried.queued_action is None
        assert retried.model_dump().get("queued_action_committed") is False


def test_archived_running_call_recovers_under_real_queued_message_header(tmp_path):
    calls = []
    runtime = make_runtime(
        tmp_path,
        None,
        evidence_operation(calls, "interrupt"),
    )
    pending = propose_fake(runtime, data=two_node_plan())
    with pytest.raises(KeyboardInterrupt):
        runtime.executor.confirm(pending.id, 1, trusted=True)
    record = runtime.store.get(pending.id)
    running = record.task.calls[-1]
    assert running.status == "running"
    record.archived_tasks.append(record.task)
    record.task = None
    record.status = "waiting_for_user"
    record.pending_decision = None
    record.error = None
    record.queued_action = None
    record.queue_origin = None
    runtime.store.save(record)
    queued = runtime.queue_message(record.id, "Review the interrupted prior task.")
    archived = queued.archived_tasks[-1]
    calls_before = archived.calls
    pending_specs = archived.pending_specs
    receipts = receipt_bytes(queued)
    checkpoints = archived.checkpoints
    checkpoint_sha256 = archived.checkpoint_sha256
    artifacts = archived.artifacts
    budget = archived.budget_state
    messages = len(queued.messages)
    calls.clear()

    first_store, executor = reopen(runtime)
    first = first_store.get(queued.id)
    recovered = first.archived_tasks[-1]

    assert first.status == "interrupted"
    assert first.pending_decision.kind == "resume"
    assert first.queued_action is None
    assert first.queue_origin is None
    assert [call.id for call in recovered.calls] == [
        call.id for call in calls_before
    ]
    assert [call.status for call in recovered.calls] == [
        "completed",
        "interrupted",
    ]
    assert recovered.pending_specs == pending_specs
    assert recovered.checkpoints == checkpoints
    assert recovered.checkpoint_sha256 == checkpoint_sha256
    assert recovered.artifacts == artifacts
    assert recovered.budget_state == budget
    assert receipt_bytes(first) == receipts
    assert len(first.messages) == messages + 1
    assert calls == []

    second = ConversationStore(first_store.root).get(queued.id)
    assert second == first
    assert receipt_bytes(second) == receipts
    assert calls == []


def test_admission_only_retry_after_prior_retry_restores_exact_failed_origin(
    tmp_path, monkeypatch
):
    runtime, queued, calls = queue_committed_action(tmp_path, "retry")
    failed_again = runtime.executor._run(
        queued.id,
        "retry",
        queued.task.id,
    )
    assert failed_again.status == "failed"
    assert failed_again.pending_decision.kind == "retry"
    assert failed_again.task.nodes[-1].status == "failed"
    decisions = failed_again.task.decisions
    messages = len(failed_again.messages)
    calls.clear()
    manager = ConversationManager(
        runtime,
        PlanningScheduler(1, 0),
        ExecutionTurnScheduler(1),
    )
    monkeypatch.setattr(
        manager,
        "_schedule",
        lambda record, reservation, operation: record.model_copy(deep=True),
    )
    try:
        admitted = manager.decide(
            failed_again.id,
            "retry",
            ConfirmationRequest(
                version=failed_again.task.baseline.version,
                trusted=True,
            ),
        )
    finally:
        manager.close()
    assert admitted.status == "queued"
    assert admitted.queued_action == "retry"
    assert admitted.model_dump().get("queued_action_committed") is False

    first_store, executor = reopen(runtime)
    first = first_store.get(admitted.id)

    assert first.status == "failed"
    assert first.pending_decision.kind == "retry"
    assert first.task.nodes[-1].status == "failed"
    assert first.task.decisions == decisions
    assert len(first.messages) == messages + 1
    assert first.model_dump().get("queued_action_committed") is False
    assert calls == []

    second = ConversationStore(first_store.root).get(admitted.id)
    assert second == first
    assert calls == []


def test_committed_retry_failure_recovers_as_actionable_retry(tmp_path, monkeypatch):
    calls = []
    fail = {"enabled": True}

    def operation(context, inputs):
        terminal = "fail" if fail["enabled"] else None
        return evidence_operation(calls, terminal)(context, inputs)

    runtime = make_runtime(tmp_path, None, operation)
    data = two_node_plan()
    data["budget"]["max_rounds"] = 5
    pending = propose_fake(runtime, data=data)
    failed = runtime.executor.confirm(pending.id, 1, trusted=True)
    assert failed.status == "failed"
    queued, task_id = runtime._queue_decision(
        failed.id,
        "retry",
        version=failed.task.baseline.version,
        trusted=True,
    )
    assert task_id == queued.task.id
    save = runtime.store.save

    def crash_after_committed_failure(record):
        save(record)
        if (
            record.queued_action == "retry"
            and any(node.status == "failed" for node in record.task.nodes)
        ):
            raise KeyboardInterrupt("Crash before clearing committed retry")

    monkeypatch.setattr(runtime.store, "save", crash_after_committed_failure)
    with pytest.raises(KeyboardInterrupt):
        runtime.executor._run(queued.id, "retry", task_id)
    crashed = runtime.store.get(queued.id)
    assert crashed.task.nodes[-1].status == "failed"
    task_state = preserved_task_state(crashed)
    receipts = receipt_bytes(crashed)
    decisions = crashed.task.decisions
    messages = len(crashed.messages)
    calls.clear()
    fail["enabled"] = False

    first_store, executor = reopen(runtime)
    first = first_store.get(queued.id)

    assert first.status == "interrupted"
    assert first.pending_decision.kind == "retry"
    assert first.task.decisions == decisions
    assert preserved_task_state(first) == task_state
    assert receipt_bytes(first) == receipts
    assert len(first.messages) == messages + 1
    assert first.model_dump().get("queued_action_committed") is False
    assert calls == []

    second = ConversationStore(first_store.root).get(queued.id)
    assert second == first
    retried = executor.retry(
        second.id,
        second.task.baseline.version,
        trusted=True,
    )
    assert retried.status == "completed"
    assert len(calls) == 1
    assert retried.queued_action is None
    assert retried.model_dump().get("queued_action_committed") is False
