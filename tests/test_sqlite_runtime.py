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
