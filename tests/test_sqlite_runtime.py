import sqlite3

import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from tests.test_agent_runtime import confirm, propose, setup


def test_proposal_persists_without_creating_unconfirmed_task_directory(tmp_path):
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.agent_runtime import AgentExecutor
    from flight_agent.verification.resources import ResourceRegistry
    from tests.agent_helpers import register_rmse

    store = SQLiteConversationStore(tmp_path / "state")
    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, [])
    executor = AgentExecutor(store, tools, ResourceRegistry())
    pending = propose(executor, store.create())
    assert store.get(pending.id).task == pending.task
    assert not store.default_task_root(pending.id).exists()


def test_external_operation_sees_committed_sqlite_dispatch_and_charged_budget(tmp_path):
    seen = []

    def operation(context, inputs):
        with sqlite3.connect(store.database_path) as reader:
            row = reader.execute(
                "SELECT id,status,spec_pending FROM calls WHERE id=?", (context.call_id,)
            ).fetchone()
            assert row == (context.call_id, "running", 1)
            assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
        saved = store.get(pending.id)
        assert saved.task.budget_state.calls == 1
        assert saved.task.budget_state.rounds == 1
        assert saved.task.baseline.confirmed
        seen.append(context.call_id)
        return CapabilityOutcome()

    executor, store, record, _ = setup(tmp_path, operation=operation)
    assert isinstance(store, SQLiteConversationStore)
    pending = propose(executor, record)
    done = confirm(executor, pending)
    assert seen == [done.task.calls[0].id]


def test_terminal_state_and_progress_are_one_commit(tmp_path, monkeypatch):
    executor, store, record, calls = setup(tmp_path)
    pending = propose(executor, record)
    progress = executor._progress
    observed = []

    def inspect_before_progress(task, run, budget):
        assert task.calls[0].status == "completed"
        with sqlite3.connect(store.database_path) as outside:
            assert outside.execute(
                "SELECT status FROM calls WHERE id=?", (run.id,)
            ).fetchone()[0] == "running"
        observed.append(run.id)
        progress(task, run, budget)
        persisted = store.get(pending.id)
        assert persisted.task.calls[0].status == "completed"
        assert run.id in persisted.task.progress_calls
        with sqlite3.connect(store.database_path) as outside:
            assert outside.execute(
                "SELECT status FROM calls WHERE id=?", (run.id,)
            ).fetchone()[0] == "running"

    monkeypatch.setattr(executor, "_progress", inspect_before_progress)
    done = confirm(executor, pending)
    assert done.status == "completed", done.error
    assert observed == calls
    saved = store.get(done.id)
    assert saved.task.calls[0].status == "completed"
    assert saved.task.analysis_runs[0].id in saved.task.progress_calls
    assert saved.task.budget_state.no_progress == 0


def test_crash_in_acceptance_rolls_back_node_receipt_and_progress(tmp_path, monkeypatch):
    executor, store, record, calls = setup(tmp_path)
    pending = propose(executor, record)
    progress = executor._progress

    def crash(task, run, budget):
        progress(task, run, budget)
        raise KeyboardInterrupt("after progress save before transaction commit")

    monkeypatch.setattr(executor, "_progress", crash)
    with pytest.raises(KeyboardInterrupt):
        confirm(executor, pending)
    saved = store.get(pending.id)
    assert saved.task.calls[0].status == "running"
    assert saved.task.nodes[0].status == "running"
    assert saved.task.analysis_runs == []
    assert saved.task.progress_calls == []
    assert saved.task.budget_state.calls == 1
    restarted_store = SQLiteConversationStore(store.root)
    recovery = type(executor)(restarted_store, executor.tools, executor.resources)
    recovered = recovery.resume(pending.id)
    assert recovered.status == "completed", recovered.error
    assert len(calls) == 1


def test_handled_acceptance_failure_does_not_resave_rolled_back_progress(tmp_path, monkeypatch):
    executor, store, record, calls = setup(tmp_path)
    pending = propose(executor, record)
    progress = executor._progress

    def fail(task, run, budget):
        progress(task, run, budget)
        raise RuntimeError("acceptance acknowledgement failed")

    monkeypatch.setattr(executor, "_progress", fail)
    interrupted = confirm(executor, pending)
    assert interrupted.status == "interrupted"
    saved = store.get(pending.id)
    assert saved.task.calls[0].status == "interrupted"
    assert saved.task.analysis_runs == []
    assert saved.task.progress_calls == []
    assert saved.task.budget_state.calls == 1
    recovery = type(executor)(
        SQLiteConversationStore(store.root), executor.tools, executor.resources,
    )
    recovered = recovery.resume(pending.id)
    assert recovered.status == "completed", recovered.error
    assert len(calls) == 1


def test_budget_exhaustion_during_invocation_keeps_original_call_recoverable(tmp_path):
    from flight_agent.runtime.budget import BudgetExhausted

    invoked = []

    def operation(context, inputs):
        invoked.append(context.call_id)
        raise BudgetExhausted("Budget exhausted during invocation")

    executor, store, record, _ = setup(tmp_path, operation=operation)
    interrupted = confirm(executor, propose(executor, record))
    assert interrupted.status == "interrupted"
    saved = store.get(record.id)
    call = saved.task.calls[0]
    assert call.status == "interrupted"
    assert saved.task.nodes[0].status == "interrupted"
    assert saved.task.stage == "interrupted"
    assert saved.pending_decision.kind == "resume"
    assert call.id in saved.task.pending_specs
    with pytest.raises(ValueError, match="unknown|unresolved"):
        executor.retry(record.id, 1, trusted=True)
    recovery = type(executor)(
        SQLiteConversationStore(store.root), executor.tools, executor.resources
    )
    resumed = recovery.resume(record.id)
    assert resumed.status == "interrupted"
    assert resumed.task.calls[0].id == call.id
    assert invoked == [call.id]


def test_budget_exhaustion_after_acceptance_keeps_committed_receipt_and_progress(tmp_path):
    from tests.agent_helpers import analysis_plan_data

    executor, store, record, _ = setup(
        tmp_path, operation=lambda context, inputs: CapabilityOutcome()
    )
    data = analysis_plan_data()
    data["budget"]["max_no_progress"] = 1
    done = confirm(executor, propose(executor, record, data))
    assert done.status == "failed"
    saved = SQLiteConversationStore(store.root).get(record.id)
    call = saved.task.calls[0]
    assert call.status == "completed"
    assert saved.task.nodes[0].status == "completed"
    assert call.id not in saved.task.pending_specs
    assert saved.task.analysis_runs[0].id == call.id
    assert call.id in saved.task.progress_calls
    assert saved.task.budget_state.no_progress == 1
    assert saved.task.result.execution_status == "partial"
    assert saved.pending_decision.kind == "clarify"
    assert saved.pending_decision.message != saved.error
    assert "revised budget" in saved.pending_decision.message
    assert len(saved.pending_decision.message) <= 4096
    from flight_agent.verification.coordinator import EngineeringRuntime

    coordinator = EngineeringRuntime(store, None, executor, executor.resources)
    queued = coordinator.queue_message(record.id, "Prepare a new plan with a revised budget")
    assert queued.status == "planning"
    assert queued.task.calls[0].status == "completed"
    replacement = propose(executor, queued, data)
    assert replacement.status == "waiting_for_user"
    assert replacement.archived_tasks[-1].calls[0].id == call.id


def test_budget_subclass_after_acceptance_never_rewrites_terminal_call(tmp_path, monkeypatch):
    from flight_agent.verification.agent_runtime import PreDispatchBudgetExhausted

    executor, store, record, _ = setup(tmp_path)
    progress = executor._progress

    def exhaust_after_progress(task, run, budget):
        progress(task, run, budget)
        raise PreDispatchBudgetExhausted("Budget limit after terminal acceptance")

    monkeypatch.setattr(executor, "_progress", exhaust_after_progress)
    done = confirm(executor, propose(executor, record))
    saved = store.get(record.id)
    assert done.status == "failed"
    assert saved.task.calls[0].status == "completed"
    assert saved.task.nodes[0].status == "completed"
    assert saved.task.calls[0].id not in saved.task.pending_specs
    assert saved.task.calls[0].id in saved.task.progress_calls


def test_budget_exhaustion_during_reconciliation_keeps_call_interrupted(tmp_path, monkeypatch):
    from flight_agent.runtime.budget import BudgetExhausted
    from flight_agent.verification.agent_runtime import ExecutionUncertain

    def operation(context, inputs):
        raise ExecutionUncertain("dispatch acknowledgement missing")

    executor, store, record, _ = setup(tmp_path, operation=operation)
    interrupted = confirm(executor, propose(executor, record))
    original = interrupted.task.calls[0].id

    def exhausted(*args):
        raise BudgetExhausted("Budget exhausted during reconciliation")

    monkeypatch.setattr(executor.tools, "reconcile", exhausted)
    resumed = executor.resume(record.id)
    assert resumed.status == "interrupted"
    assert resumed.task.calls[0].status == "interrupted"
    assert resumed.task.calls[0].id == original
    assert original in resumed.task.pending_specs
    assert store.get(record.id) == resumed


def test_budget_exhaustion_after_recovery_acceptance_keeps_committed_receipt(tmp_path, monkeypatch):
    from tests.agent_helpers import analysis_plan_data

    invoked = []

    def operation(context, inputs):
        invoked.append(context.call_id)
        return CapabilityOutcome()

    executor, store, record, _ = setup(tmp_path, operation=operation)
    data = analysis_plan_data()
    data["budget"]["max_no_progress"] = 1
    pending = propose(executor, record, data)

    def crash(*args):
        raise KeyboardInterrupt("after receipt publication")

    monkeypatch.setattr(executor, "_accept", crash)
    with pytest.raises(KeyboardInterrupt):
        confirm(executor, pending)
    recovery = type(executor)(
        SQLiteConversationStore(store.root), executor.tools, executor.resources
    )
    result = recovery.resume(record.id)
    saved = store.get(record.id)
    assert result.status == "failed"
    assert saved.task.calls[0].status == "completed"
    assert saved.task.nodes[0].status == "completed"
    assert saved.task.calls[0].id not in saved.task.pending_specs
    assert saved.task.calls[0].id in saved.task.progress_calls
    assert saved.task.analysis_runs[0].id == saved.task.calls[0].id
    assert saved.task.budget_state.no_progress == 1
    assert invoked == [saved.task.calls[0].id]


@pytest.mark.parametrize("damage", ["future", "corrupt"])
def test_web_startup_refuses_unusable_database_without_json_fallback(tmp_path, damage):
    from flight_agent.verification.sqlite_schema import DatabaseStartupError
    from flight_agent.web import create_web_app

    store = SQLiteConversationStore(tmp_path / "runs" / "verification", require_empty=True)
    if damage == "future":
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("PRAGMA user_version=999")
    else:
        store.database_path.write_bytes(b"not a SQLite database")
    with pytest.raises(DatabaseStartupError):
        create_web_app(tmp_path)
    assert not list(store.root.rglob("conversation.json"))


def test_web_startup_rejects_corruption_in_unqueried_table_without_rewriting(tmp_path):
    from flight_agent.verification.sqlite_schema import DatabaseStartupError
    from flight_agent.web import create_web_app

    store = SQLiteConversationStore(tmp_path / "runs" / "verification", require_empty=True)
    store.create()
    with sqlite3.connect(store.database_path) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        root_page = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='task_events'"
        ).fetchone()[0]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = bytearray(store.database_path.read_bytes())
    assert root_page > 1
    data[(root_page - 1) * page_size] = 0
    store.database_path.write_bytes(data)
    damaged = store.database_path.read_bytes()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1

    with pytest.raises(DatabaseStartupError, match="integrity|corrupt"):
        create_web_app(tmp_path)

    assert store.database_path.read_bytes() == damaged
    assert not list(store.root.rglob("conversation.json"))


@pytest.mark.parametrize("damage", ["future", "corrupt"])
def test_web_startup_refusal_does_not_recover_live_hot_journal(tmp_path, damage):
    import os
    import subprocess
    import sys

    from flight_agent.verification.sqlite_schema import DatabaseStartupError
    from flight_agent.web import create_web_app

    store = SQLiteConversationStore(tmp_path / "runs" / "verification", require_empty=True)
    store.create()
    connection = sqlite3.connect(store.database_path)
    assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    connection.execute("CREATE TABLE pressure (id INTEGER PRIMARY KEY, payload BLOB)")
    connection.executemany(
        "INSERT INTO pressure (payload) VALUES (?)", [(b"a" * 2000,)] * 300
    )
    if damage == "future":
        connection.execute("PRAGMA user_version=999")
    connection.commit()
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    root_page = connection.execute(
        "SELECT rootpage FROM sqlite_master WHERE name='task_events'"
    ).fetchone()[0]
    connection.close()

    script = """
import os
import sqlite3
connection = sqlite3.connect(os.environ["DATABASE"], isolation_level=None)
connection.execute("PRAGMA cache_size=8")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE pressure SET payload=?", (b"b" * 2000,))
os._exit(73)
"""
    environment = dict(os.environ, DATABASE=str(store.database_path))
    process = subprocess.run(
        [sys.executable, "-c", script], env=environment, timeout=15, capture_output=True
    )
    assert process.returncode == 73, process.stderr
    journal = store.root / "conversations.sqlite3-journal"
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    if damage == "corrupt":
        data = bytearray(store.database_path.read_bytes())
        data[(root_page - 1) * page_size] = 0
        store.database_path.write_bytes(data)

    def inventory():
        return {
            str(path.relative_to(store.root)): path.read_bytes() if path.is_file() else None
            for path in store.root.rglob("*")
        }

    before = inventory()
    with pytest.raises(DatabaseStartupError, match="newer|integrity|corrupt"):
        create_web_app(tmp_path)
    assert inventory() == before


def test_process_exit_after_dispatch_keeps_original_unretryable_call(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.agent_runtime import AgentExecutor
    from flight_agent.verification.resources import ResourceRegistry
    from tests.agent_helpers import register_rmse

    script = """
import os
from pathlib import Path
from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from tests.agent_helpers import analysis_plan_data, register_rmse

root = Path(os.environ["FLIGHT_TEST_STATE"])
store = SQLiteConversationStore(root, require_empty=True)
tools = CapabilityRegistry(agent_mode=True)
def stop(context, inputs):
    os._exit(73)
register_rmse(tools, [], operation=stop)
executor = AgentExecutor(store, tools, ResourceRegistry())
record = executor.propose(store.create(), AgentPlan.model_validate(analysis_plan_data()))
executor.confirm(record.id, 1, trusted=True)
"""
    environment = dict(os.environ)
    environment["FLIGHT_TEST_STATE"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = os.pathsep.join([str(Path.cwd() / "src"), str(Path.cwd())])
    process = subprocess.run(
        [sys.executable, "-c", script], env=environment, timeout=15, check=False,
        capture_output=True, text=True,
    )
    assert process.returncode == 73, process.stderr
    store = SQLiteConversationStore(tmp_path / "state")
    record = store.recent()[0]
    original = record.task.calls[0].id
    assert record.status == "interrupted"
    assert record.task.calls[0].status == "interrupted"
    assert record.task.budget_state.calls == 1
    assert original in record.task.pending_specs
    operations = []
    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, operations)
    executor = AgentExecutor(store, tools, ResourceRegistry())
    with pytest.raises(ValueError, match="unknown|unresolved"):
        executor.retry(record.id, 1, trusted=True)
    blocked = executor.resume(record.id)
    assert blocked.status == "interrupted"
    assert blocked.task.calls[0].id == original
    assert operations == []


def test_failed_message_insert_rolls_back_header_transition(tmp_path):
    _executor, store, record, _ = setup(tmp_path)
    before = store.get(record.id)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_user_message BEFORE INSERT ON messages
            WHEN NEW.role='user'
            BEGIN SELECT RAISE(ABORT, 'injected message failure'); END
        """)
    from flight_agent.verification.contracts import ConversationMessage
    from flight_agent.verification.store import now

    record.status = "planning"
    record.messages.append(ConversationMessage(
        role="user", content="Analyze", created_at=now(),
    ))
    with pytest.raises(OSError, match="injected message failure"):
        store.save(record)
    assert store.get(record.id) == before
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
