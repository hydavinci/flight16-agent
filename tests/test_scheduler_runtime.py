from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling import ExecutionTurnScheduler, PlanningScheduler
from flight_agent.verification.agent_planner import AgentDecision
from flight_agent.verification.contracts import PendingDecision
from flight_agent.verification.coordinator import ConversationActive
from flight_agent.verification.routes import (
    ConversationManager,
    MessageRequest,
)
from flight_agent.verification.store import ConversationStore
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


def conversation_manager(runtime):
    return ConversationManager(
        runtime,
        PlanningScheduler(1, 0),
        ExecutionTurnScheduler(1),
    )


def archive_unknown_task(runtime):
    pending = propose_fake(runtime)
    with pytest.raises(KeyboardInterrupt):
        runtime.executor.confirm(pending.id, 1, trusted=True)
    record = runtime.store.get(pending.id)
    assert record.task.calls[-1].status == "running"
    record.archived_tasks.append(record.task)
    record.task = None
    record.status = "waiting_for_user"
    record.pending_decision = None
    record.error = None
    record.queued_action = None
    record.queued_action_committed = False
    record.queue_origin = None
    record.turn_origin = None
    runtime.store.save(record)
    return record


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


def test_stale_planner_result_cannot_overwrite_newer_record(tmp_path):
    entered, release = Event(), Event()

    class Planner:
        def propose(self, message, conversation, **kwargs):
            entered.set()
            assert release.wait(5)
            return AgentDecision(action="answer", text="Stale answer")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "Question")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(runtime.continue_submission, queued.id)
        assert entered.wait(5)
        with runtime.locks.hold(queued.id):
            newer = runtime.store.get(queued.id)
            newer.status = "waiting_for_user"
            newer.queued_action = None
            newer.queue_origin = None
            newer.error = "newer planner state"
            newer.pending_decision = PendingDecision(
                kind="clarify", message="Newer clarification"
            )
            runtime.store.save(newer)
        release.set()
        with pytest.raises(ConversationActive, match="changed"):
            future.result(timeout=5)
    saved = runtime.store.get(queued.id)
    assert saved.status == "waiting_for_user"
    assert saved.error == "newer planner state"
    assert saved.pending_decision.message == "Newer clarification"
    assert saved.task is None


def test_matching_planner_exception_clears_reservation(tmp_path):
    class Planner:
        def propose(self, message, conversation, **kwargs):
            raise RuntimeError("Planner unavailable")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "Question")
    with pytest.raises(RuntimeError, match="Planner unavailable"):
        runtime.continue_submission(queued.id)
    saved = runtime.store.get(queued.id)
    assert saved.status == "failed"
    assert saved.error == "Planner unavailable"
    assert saved.pending_decision.kind == "clarify"
    assert saved.pending_decision.message == saved.error
    assert saved.queued_action is None
    assert saved.queue_origin is None


def test_archived_only_unresolved_planner_failure_requires_reconciliation(tmp_path):
    class Planner:
        def propose(self, message, conversation, **kwargs):
            raise RuntimeError("Planner unavailable")

    def operation(context, inputs):
        raise KeyboardInterrupt("Unknown external result")

    runtime = make_runtime(tmp_path, Planner(), operation)
    record = archive_unknown_task(runtime)

    runtime.queue_message(record.id, "Plan without replaying archived work")
    with pytest.raises(RuntimeError, match="Planner unavailable"):
        runtime.continue_submission(record.id)
    saved = runtime.store.get(record.id)

    assert saved.status == "interrupted"
    assert saved.pending_decision.kind == "clarify"
    assert "reconcil" in saved.pending_decision.message.lower()
    assert "resume" not in saved.pending_decision.message.lower()
    assert saved.task is None
    assert saved.archived_tasks[-1].calls[-1].status == "running"
    assert saved.queued_action is None
    assert saved.queue_origin is None


def test_manager_failure_fallback_checks_archived_unresolved_calls(tmp_path):
    def operation(context, inputs):
        raise KeyboardInterrupt("Unknown external result")

    runtime = make_runtime(tmp_path, None, operation)
    record = archive_unknown_task(runtime)
    record = runtime.store.get(record.id)
    record.status = "planning"
    runtime.store.save(record)
    manager = conversation_manager(runtime)
    try:
        manager._fail(record.id, RuntimeError("Planner unavailable"))
    finally:
        manager.close()
    saved = runtime.store.get(record.id)

    assert saved.status == "interrupted"
    assert saved.pending_decision.kind == "clarify"
    assert "reconcil" in saved.pending_decision.message.lower()
    assert saved.task is None


def test_planner_failure_is_one_atomic_coherent_transition(
    tmp_path, monkeypatch
):
    entered = Event()
    release_planner = Event()
    terminal_saved = Event()
    release_terminal_save = Event()

    class Planner:
        def propose(self, message, conversation, **kwargs):
            entered.set()
            assert release_planner.wait(5)
            raise RuntimeError("Planner unavailable")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    manager = conversation_manager(runtime)
    original_save = runtime.store.save

    def blocking_save(record):
        original_save(record)
        if (
            record.status in ("failed", "interrupted")
            and not terminal_saved.is_set()
        ):
            terminal_saved.set()
            assert release_terminal_save.wait(5)

    monkeypatch.setattr(runtime.store, "save", blocking_save)
    try:
        queued = manager.submit(
            None,
            MessageRequest(message="Question with a failing planner"),
        )
        assert entered.wait(5)
        planning = runtime.store.get(queued.id)
        assert planning.status == "planning"
        assert planning.pending_decision is None

        release_planner.set()
        assert terminal_saved.wait(5)
        visible = runtime.store.get(queued.id)
        assert visible.status == "failed"
        assert visible.pending_decision.kind == "clarify"
        assert visible.pending_decision.message == visible.error
        assert visible.queued_action is None
        assert visible.queue_origin is None
        messages = visible.messages
    finally:
        release_planner.set()
        release_terminal_save.set()
        manager.close()

    reopened = ConversationStore(runtime.store.root).get(queued.id)
    assert reopened == runtime.store.get(queued.id)
    assert reopened.messages == messages
    assert not any(
        message.role == "assistant"
        and message.content.startswith("Request failed:")
        for message in reopened.messages
    )


def test_manager_stale_planner_failure_cannot_overwrite_newer_turn(tmp_path):
    entered = Event()
    release = Event()

    class Planner:
        def propose(self, message, conversation, **kwargs):
            entered.set()
            assert release.wait(5)
            raise RuntimeError("Stale planner failure")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    manager = conversation_manager(runtime)
    try:
        queued = manager.submit(
            None,
            MessageRequest(message="Question with a stale planner"),
        )
        assert entered.wait(5)
        newer = runtime.store.get(queued.id)
        newer.status = "waiting_for_user"
        newer.pending_decision = PendingDecision(
            kind="clarify",
            message="Newer clarification",
        )
        newer.error = "newer planned state"
        newer.queued_action = None
        newer.queued_action_committed = False
        newer.queue_origin = None
        newer.turn_origin = None
        runtime.store.save(newer)
        release.set()
    finally:
        release.set()
        manager.close()
    saved = runtime.store.get(queued.id)

    assert saved.status == newer.status
    assert saved.error == "newer planned state"
    assert saved.pending_decision == newer.pending_decision
    assert saved.task is None


def test_stale_planner_exception_cannot_overwrite_newer_record(tmp_path):
    entered, release = Event(), Event()

    class Planner:
        def propose(self, message, conversation, **kwargs):
            entered.set()
            assert release.wait(5)
            raise RuntimeError("Stale planner failure")

    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "Question")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(runtime.continue_submission, queued.id)
        assert entered.wait(5)
        with runtime.locks.hold(queued.id):
            newer = runtime.store.get(queued.id)
            newer.status = "waiting_for_user"
            newer.queued_action = None
            newer.queue_origin = None
            newer.error = "newer planner state"
            runtime.store.save(newer)
        release.set()
        with pytest.raises(RuntimeError, match="Stale planner failure"):
            future.result(timeout=5)
    saved = runtime.store.get(queued.id)
    assert saved.status == "waiting_for_user"
    assert saved.error == "newer planner state"


def test_matching_resource_exception_clears_reservation(tmp_path, monkeypatch):
    runtime = make_runtime(
        tmp_path, None, lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "Question")
    with runtime.locks.hold(queued.id):
        record = runtime.store.get(queued.id)
        record.project_root = str(tmp_path)
        record.resource_location = "missing"
        runtime.store.save(record)

    def unavailable(location, locator):
        raise OSError("Resource unavailable")

    monkeypatch.setattr(runtime.resources, "inspect", unavailable)
    with pytest.raises(OSError, match="Resource unavailable"):
        runtime.continue_submission(queued.id)
    saved = runtime.store.get(queued.id)
    assert saved.status == "failed"
    assert saved.error == "Resource unavailable"
    assert saved.queued_action is None
    assert saved.queue_origin is None


def test_stale_resource_exception_cannot_overwrite_newer_record(
    tmp_path, monkeypatch
):
    entered, release = Event(), Event()
    runtime = make_runtime(
        tmp_path, None, lambda context, inputs: CapabilityOutcome()
    )
    queued = runtime.queue_message(None, "Question")
    with runtime.locks.hold(queued.id):
        record = runtime.store.get(queued.id)
        record.project_root = str(tmp_path)
        record.resource_location = "missing"
        runtime.store.save(record)

    def unavailable(location, locator):
        entered.set()
        assert release.wait(5)
        raise OSError("Stale resource failure")

    monkeypatch.setattr(runtime.resources, "inspect", unavailable)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(runtime.continue_submission, queued.id)
        assert entered.wait(5)
        with runtime.locks.hold(queued.id):
            newer = runtime.store.get(queued.id)
            newer.status = "waiting_for_user"
            newer.queued_action = None
            newer.queue_origin = None
            newer.error = "newer resource state"
            runtime.store.save(newer)
        release.set()
        with pytest.raises(OSError, match="Stale resource failure"):
            future.result(timeout=5)
    saved = runtime.store.get(queued.id)
    assert saved.status == "waiting_for_user"
    assert saved.error == "newer resource state"


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


def test_stale_capability_result_cannot_overwrite_newer_record(tmp_path):
    entered, release = Event(), Event()

    def operation(context, inputs):
        entered.set()
        assert release.wait(5)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            runtime.executor.confirm, pending.id, 1, trusted=True
        )
        assert entered.wait(5)
        with runtime.locks.hold(pending.id):
            newer = runtime.store.get(pending.id)
            budget = newer.task.budget_state.model_copy(deep=True)
            pending_specs = newer.task.pending_specs.copy()
            newer.error = "newer capability state"
            newer.pending_decision = PendingDecision(
                kind="clarify", message="Newer capability follow-up"
            )
            newer.task.calls[0].error = "newer call state"
            newer.task.nodes[0].error = "newer node state"
            stage = newer.task.stage
            runtime.store.save(newer)
            call = newer.task.calls[0].model_copy(deep=True)
            node = newer.task.nodes[0].model_copy(deep=True)
        release.set()
        result = future.result(timeout=5)
    saved = runtime.store.get(pending.id)
    assert result.status == saved.status == "running"
    assert saved.queued_action == "confirm"
    assert result.error == saved.error == "newer capability state"
    assert saved.pending_decision.message == "Newer capability follow-up"
    assert saved.task.calls[0] == call
    assert saved.task.nodes[0] == node
    assert saved.task.stage == stage
    assert saved.task.budget_state == budget
    assert saved.task.pending_specs == pending_specs
    assert saved.task.analysis_runs == []
    assert saved.task.outputs == {}
    assert saved.task.result is None


def test_stale_capability_exception_cannot_apply_failure_or_budget(tmp_path):
    entered, release = Event(), Event()

    def operation(context, inputs):
        entered.set()
        assert release.wait(5)
        context.budget.charge_round(key="stale-callback")
        raise RuntimeError("Stale capability failure")

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            runtime.executor.confirm, pending.id, 1, trusted=True
        )
        assert entered.wait(5)
        with runtime.locks.hold(pending.id):
            newer = runtime.store.get(pending.id)
            budget = newer.task.budget_state.model_copy(deep=True)
            pending_specs = newer.task.pending_specs.copy()
            newer.error = "newer capability state"
            newer.pending_decision = PendingDecision(
                kind="clarify", message="Newer capability follow-up"
            )
            newer.task.calls[0].error = "newer call state"
            newer.task.nodes[0].error = "newer node state"
            stage = newer.task.stage
            runtime.store.save(newer)
            call = newer.task.calls[0].model_copy(deep=True)
            node = newer.task.nodes[0].model_copy(deep=True)
        release.set()
        result = future.result(timeout=5)
    saved = runtime.store.get(pending.id)
    assert result.status == saved.status == "running"
    assert saved.queued_action == "confirm"
    assert result.error == saved.error == "newer capability state"
    assert saved.pending_decision.message == "Newer capability follow-up"
    assert saved.task.calls[0] == call
    assert saved.task.nodes[0] == node
    assert saved.task.stage == stage
    assert saved.task.budget_state == budget
    assert saved.task.pending_specs == pending_specs
    assert saved.task.analysis_runs == []
    assert saved.task.outputs == {}
    assert saved.task.result is None


@pytest.mark.parametrize("kind", ["clarify", "capability_gap"])
@pytest.mark.parametrize("action", ["confirm", "resume", "retry"])
def test_decide_preserves_pending_followup_guards(tmp_path, kind, action):
    runtime = make_runtime(
        tmp_path, None, lambda context, inputs: CapabilityOutcome()
    )
    pending = propose_fake(runtime)
    completed = runtime.executor.confirm(pending.id, 1, trusted=True)
    with runtime.locks.hold(completed.id):
        record = runtime.store.get(completed.id)
        record.status = "waiting_for_user"
        record.pending_decision = PendingDecision(
            kind=kind, message="Address this follow-up"
        )
        runtime.store.save(record)
    kwargs = (
        {"version": 1, "trusted": True}
        if action in ("confirm", "retry")
        else {}
    )
    with pytest.raises(
        ValueError,
        match=(
            "new unconfirmed baseline"
            if action == "confirm"
            else "current follow-up"
        ),
    ):
        runtime.decide(completed.id, action, **kwargs)
    saved = runtime.store.get(completed.id)
    assert saved.status == "waiting_for_user"
    assert saved.pending_decision.kind == kind
    assert saved.queued_action is None


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
