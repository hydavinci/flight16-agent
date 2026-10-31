import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.contracts import ConversationState
from flight_agent.verification.store import ConversationStore
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
