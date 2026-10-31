from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.agent_planner import AgentDecision
from flight_agent.verification.coordinator import ConversationActive
from tests.scheduling_helpers import make_runtime, propose_fake


def test_two_conversations_plan_concurrently(tmp_path):
    barrier = Barrier(2)

    class Planner:
        def propose(self, message, conversation, **kwargs):
            barrier.wait(timeout=5)
            return AgentDecision(action="answer", text=f"Answered {message}")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    a = runtime.queue_message(None, "First question")
    b = runtime.queue_message(None, "Second question")
    assert a.status == b.status == "queued"
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(runtime.continue_submission, item.id) for item in (a, b)
        ]
        assert [future.result(timeout=5).status for future in futures] == [
            "completed"
        ] * 2
    assert runtime.locks is runtime.executor.locks
    assert runtime.locks.entry_count == 0


def test_active_conversation_rejects_message_before_append(tmp_path):
    runtime = make_runtime(
        tmp_path, None, lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "First question")
    with pytest.raises(ConversationActive, match="admitted turn"):
        runtime.queue_message(queued.id, "Second question")
    saved = runtime.store.get(queued.id)
    assert [message.content for message in saved.messages] == ["First question"]


def test_same_conversation_confirm_is_serial_and_idempotent(tmp_path):
    entered, release, attempted = Event(), Event(), Event()
    calls = []

    def operation(context, inputs):
        calls.append(context.call_id)
        entered.set()
        assert release.wait(5)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)

    def confirm():
        return runtime.executor.confirm(pending.id, 1, trusted=True)

    def second():
        attempted.set()
        return confirm()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(confirm)
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert attempted.wait(5)
            assert not other.done()
        finally:
            release.set()
        assert first.result(timeout=5).status == "completed"
        assert other.result(timeout=5).status == "completed"
    assert len(calls) == 1
    assert len(runtime.store.get(pending.id).task.calls) == 1
    assert runtime.locks.entry_count == 0


def test_waiting_local_node_has_no_dispatched_call_or_charged_budget(tmp_path):
    entered = Event()
    runtime = make_runtime(
        tmp_path,
        None,
        lambda context, inputs: (entered.set(), CapabilityOutcome())[1],
        lane="local_matlab",
    )
    pending = propose_fake(runtime, lane="local_matlab")
    scheduler = runtime.executor.scheduler
    definition = runtime.executor.tools.definition("offline_rmse")
    metadata = runtime.executor.tools.scheduling("offline_rmse")
    saved = Event()
    original_save = runtime.store.save

    def observe(record):
        original_save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            saved.set()

    runtime.store.save = observe
    lock_available = Event()

    def probe_lock():
        with runtime.locks.hold(pending.id):
            lock_available.set()

    with ThreadPoolExecutor(2) as pool:
        with scheduler.slot(definition, metadata):
            future = pool.submit(runtime.executor.confirm, pending.id, 1, trusted=True)
            assert saved.wait(5)
            queued = runtime.store.get(pending.id)
            assert queued.status == "queued"
            assert queued.task.calls == []
            assert queued.task.budget_state.calls == 0
            assert queued.task.budget_state.rounds == 0
            assert not entered.is_set()
            probe = pool.submit(probe_lock)
            assert lock_available.wait(1)
            probe.result(timeout=1)
        assert future.result(timeout=5).status == "completed"
    assert entered.is_set()
