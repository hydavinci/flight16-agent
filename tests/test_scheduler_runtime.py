from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.agent_planner import AgentDecision
from flight_agent.verification.coordinator import ConversationActive
from tests.agent_helpers import analysis_plan_data
from tests.scheduling_helpers import make_runtime, propose_fake


def assert_conversation_lock_available(runtime, conversation_id):
    acquired = Event()

    def probe():
        with runtime.locks.hold(conversation_id):
            acquired.set()

    thread = Thread(target=probe, daemon=True)
    thread.start()
    assert acquired.wait(1)
    thread.join(timeout=1)
    assert not thread.is_alive()


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


def test_confirm_reservation_rejects_message_and_deduplicates_before_run(
    tmp_path, monkeypatch
):
    entered, release = Event(), Event()
    calls = []

    def operation(context, inputs):
        calls.append(context.call_id)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)
    run = runtime.executor._run

    def paused_run(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return run(*args, **kwargs)

    monkeypatch.setattr(runtime.executor, "_run", paused_run)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(
            runtime.decide,
            pending.id,
            "confirm",
            version=1,
            trusted=True,
        )
        try:
            assert entered.wait(5)
            with pytest.raises(ConversationActive, match="admitted turn"):
                runtime.queue_message(pending.id, "Competing message")
            duplicate = runtime.decide(
                pending.id,
                "confirm",
                version=1,
                trusted=True,
            )
            assert duplicate.status == "queued"
            assert duplicate.queued_action == "confirm"
            with pytest.raises(ValueError, match="exact current baseline"):
                runtime.decide(
                    pending.id,
                    "confirm",
                    version=2,
                    trusted=True,
                )
            with pytest.raises(ValueError, match="admitted turn"):
                runtime.decide(pending.id, "resume")
        finally:
            release.set()
        assert first.result(timeout=5).status == "completed"
    assert len(calls) == 1
    assert len(runtime.store.get(pending.id).task.calls) == 1
    assert runtime.locks.entry_count == 0


def test_planner_runs_without_holding_conversation_lock(tmp_path):
    runtime = None

    class Planner:
        def propose(self, message, conversation, **kwargs):
            assert_conversation_lock_available(runtime, conversation.id)
            return AgentDecision(action="answer", text="Answered")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "Question")
    assert runtime.continue_submission(queued.id).status == "completed"


def test_capability_runs_without_holding_conversation_lock(tmp_path):
    runtime = None
    identifier = None

    def operation(context, inputs):
        assert_conversation_lock_available(runtime, identifier)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)
    identifier = pending.id
    assert runtime.executor.confirm(pending.id, 1, trusted=True).status == "completed"


def test_capability_checkpoint_mutation_uses_conversation_lock(tmp_path):
    entered, released, saving = Event(), Event(), Event()

    def operation(context, inputs):
        entered.set()
        assert released.wait(5)
        saving.set()
        context.save_checkpoint({"phase": "saved"})
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            runtime.executor.confirm, pending.id, 1, trusted=True
        )
        assert entered.wait(5)
        with runtime.locks.hold(pending.id):
            released.set()
            assert saving.wait(5)
            assert not future.done()
        assert future.result(timeout=5).status == "completed"
    saved = runtime.store.get(pending.id)
    assert saved.task.checkpoints


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


def test_queued_task_replacement_is_rejected_before_dispatch(tmp_path):
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
    queued = Event()
    original_save = runtime.store.save

    def observe(record):
        original_save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            queued.set()

    runtime.store.save = observe
    with ThreadPoolExecutor(1) as pool:
        with scheduler.slot(definition, metadata):
            future = pool.submit(
                runtime.executor.confirm, pending.id, 1, trusted=True
            )
            assert queued.wait(5)
            with runtime.locks.hold(pending.id):
                record = runtime.store.get(pending.id)
                original = record.task
                replacement = original.model_copy(deep=True)
                replacement.id = "b" * 32
                replacement.result_directory = str(
                    runtime.store.default_task_root(record.id) / replacement.id
                )
                Path(replacement.result_directory).mkdir()
                record.archived_tasks.append(original)
                record.task = replacement
                runtime.store.save(record)
        future.result(timeout=5)
    saved = runtime.store.get(pending.id)
    tasks = [*saved.archived_tasks, saved.task]
    assert not entered.is_set()
    assert all(task.calls == [] for task in tasks)
    assert all(task.budget_state.calls == 0 for task in tasks)
    assert all(task.budget_state.rounds == 0 for task in tasks)


def test_queued_dependency_mutation_is_rejected_before_dispatch(tmp_path):
    from flight_agent.runtime.agent_contracts import AgentBaseline
    from flight_agent.runtime.contracts import fingerprint

    entered = Event()
    runtime = make_runtime(
        tmp_path,
        None,
        lambda context, inputs: (entered.set(), CapabilityOutcome())[1],
        lane="local_matlab",
    )
    data = analysis_plan_data()
    data["allowed_effects"] = ["execute_project"]
    other = deepcopy(data["nodes"][0])
    other["id"] = "other"
    data["nodes"].append(other)
    pending = propose_fake(runtime, lane="local_matlab", data=data)
    scheduler = runtime.executor.scheduler
    definition = runtime.executor.tools.definition("offline_rmse")
    metadata = runtime.executor.tools.scheduling("offline_rmse")
    queued = Event()
    original_save = runtime.store.save

    def observe(record):
        original_save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            queued.set()

    runtime.store.save = observe
    with ThreadPoolExecutor(1) as pool:
        with scheduler.slot(definition, metadata):
            future = pool.submit(
                runtime.executor.confirm, pending.id, 1, trusted=True
            )
            assert queued.wait(5)
            with runtime.locks.hold(pending.id):
                record = runtime.store.get(pending.id)
                record.task.nodes[0].depends_on = ["other"]
                baseline = record.task.baseline.model_dump(mode="json")
                baseline["nodes"][0]["depends_on"] = ["other"]
                record.task.baseline = AgentBaseline.model_validate(baseline)
                record.task.baseline_sha256 = fingerprint(
                    record.task.baseline.model_dump(mode="json")
                )
                runtime.store.save(record)
        future.result(timeout=5)
    saved = runtime.store.get(pending.id)
    assert not entered.is_set()
    assert saved.task.calls == []
    assert saved.task.budget_state.calls == 0
    assert saved.task.budget_state.rounds == 0
