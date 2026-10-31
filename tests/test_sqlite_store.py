import hashlib
import json
import sqlite3
import threading

import pytest

from flight_agent.runtime.agent_contracts import BudgetState
from flight_agent.runtime.evidence import describe_evidence, persist_agent_run
from flight_agent.runtime.observations import OutputArtifact
from flight_agent.verification.agent_runtime import accept_verified_run
from flight_agent.verification.contracts import (
    ConversationMessage,
    ConversationState,
    PendingDecision,
    ToolCall,
)
from flight_agent.verification.sqlite_codec import canonical, encode
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from flight_agent.verification.store import now
from tests.test_agent_evidence import run as invocation_run
from tests.test_agent_history import agent_task


def test_round_trip_orders_messages_tasks_and_preserves_domain_values(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    record.task.stage = "awaiting_confirmation"
    record.pending_decision = PendingDecision(kind="confirm_baseline", message="Confirm v1")
    record.messages = [
        ConversationMessage(role="user", content="分析", created_at=now()),
        ConversationMessage(
            role="assistant", content="Confirm v1", created_at=now(), response_kind="plan"
        ),
    ]
    store.save(record)
    assert store.get(record.id) == record
    assert not list(store.root.rglob("conversation.json"))
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT baseline_version FROM tasks").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_transaction_rolls_back_message_and_state_and_event_together(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    store.save(record)
    before = store.get(record.id)
    with sqlite3.connect(store.database_path) as connection:
        event_count = connection.execute("SELECT count(*) FROM task_events").fetchone()[0]
    with pytest.raises(RuntimeError, match="power loss"):
        with store.transaction():
            record.messages.append(
                ConversationMessage(role="user", content="next", created_at=now())
            )
            record.status = "planning"
            record.task.stage = "planning"
            store.save(record)
            raise RuntimeError("power loss")
    assert store.get(record.id) == before
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == event_count


def test_nested_savepoint_rollback_preserves_outer_transaction(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    with store.transaction():
        record.messages.append(
            ConversationMessage(role="user", content="outer", created_at=now())
        )
        store.save(record)
        with pytest.raises(RuntimeError, match="nested failure"):
            with store.transaction():
                record.messages.append(
                    ConversationMessage(role="user", content="inner", created_at=now())
                )
                store.save(record)
                raise RuntimeError("nested failure")
        assert [message.content for message in store.get(record.id).messages] == ["outer"]
    assert [message.content for message in store.get(record.id).messages] == ["outer"]


@pytest.mark.parametrize("damage", ["missing", "digest"])
def test_bad_new_artifact_never_publishes_any_state(tmp_path, damage):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    path = directory / "metric.json"
    path.write_text('{"rmse":1}')
    record.task.artifacts.append(
        OutputArtifact(**describe_evidence(directory, "metric.json").model_dump())
    )
    if damage == "missing":
        path.unlink()
    else:
        path.write_text('{"rmse":9}')
    with pytest.raises(ValueError, match="missing|integrity"):
        store.save(record)
    assert store.get(record.id).task is None
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 0


def test_artifact_index_is_metadata_and_task_events_are_append_only(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    path = directory / "metric.json"
    path.write_text('{"rmse":1}')
    record.task.artifacts.append(
        OutputArtifact(
            **describe_evidence(directory, "metric.json").model_dump(),
            id="measurement",
            node_id="compute",
        )
    )
    store.save(record)
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT relative_path, size, media_type, role, descriptor_json FROM artifacts"
        ).fetchone()
        assert row[:4] == ("metric.json", path.stat().st_size, "application/json", "data")
        assert json.loads(row[4])["id"] == "measurement"
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM task_events")


def test_completed_call_rejects_stale_regression_and_preserves_receipt(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    run = invocation_run()
    call = ToolCall(
        id=run.id,
        node_id="compute",
        tool=run.spec.capability,
        tool_version=run.spec.capability_version,
        baseline_version=record.task.baseline.version,
        status="running",
        inputs=run.spec.inputs,
        started_at=run.started_at,
    )
    record.task.calls.append(call)
    record.task.pending_specs[call.id] = run.spec
    record.task.nodes[0].status = "running"
    store.save(record)
    stale = store.get(record.id)
    current = store.get(record.id)
    persist_agent_run(directory, run)
    accept_verified_run(current.task, current.task.nodes[0], current.task.calls[0], run)
    store.save(current)

    with pytest.raises(ValueError, match="terminal|immutable"):
        store.save(stale)

    saved = store.get(record.id)
    assert saved.task.calls[0].status == "completed"
    assert saved.task.analysis_runs == [run]
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT status, receipt_json FROM calls WHERE id=?", (run.id,)
        ).fetchone()
        assert row[0] == "completed"
        assert json.loads(row[1]) == run.model_dump(mode="json")


def test_recorded_call_identity_fields_are_immutable(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    run = invocation_run()
    record.task.calls.append(
        ToolCall(
            id=run.id,
            node_id="compute",
            tool=run.spec.capability,
            tool_version=run.spec.capability_version,
            baseline_version=record.task.baseline.version,
            status="running",
            inputs=run.spec.inputs,
            started_at=run.started_at,
        )
    )
    record.task.pending_specs[run.id] = run.spec
    store.save(record)
    changed = store.get(record.id)
    changed.task.calls[0].inputs = {"observed": [9.0], "reference": [0.0]}

    with pytest.raises(ValueError, match="immutable"):
        store.save(changed)

    assert store.get(record.id).task.calls[0].inputs == run.spec.inputs


def test_failed_call_retains_immutable_spec_after_pending_state_is_cleared(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    run = invocation_run()
    record.task.calls.append(
        ToolCall(
            id=run.id,
            node_id="compute",
            tool=run.spec.capability,
            tool_version=run.spec.capability_version,
            baseline_version=record.task.baseline.version,
            status="running",
            inputs=run.spec.inputs,
            started_at=run.started_at,
        )
    )
    record.task.pending_specs[run.id] = run.spec
    store.save(record)
    failed = store.get(record.id)
    failed.task.calls[0].status = "failed"
    failed.task.calls[0].error = "controlled failure"
    failed.task.calls[0].finished_at = now()
    failed.task.nodes[0].status = "failed"
    failed.task.pending_specs.pop(run.id)

    store.save(failed)

    assert store.get(record.id).task.pending_specs == {}
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT status, spec_pending, spec_json FROM calls WHERE id=?", (run.id,)
        ).fetchone()
        assert row[:2] == ("failed", 0)
        assert json.loads(row[2]) == run.spec.model_dump(mode="json")


def test_removing_persisted_artifact_is_rejected_atomically(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    path = directory / "metric.json"
    path.write_text('{"rmse":1}')
    record.task.artifacts.append(
        OutputArtifact(**describe_evidence(directory, "metric.json").model_dump())
    )
    store.save(record)
    before = store.get(record.id)
    changed = store.get(record.id)
    changed.task.artifacts.clear()

    with pytest.raises(ValueError, match="artifact|evidence"):
        store.save(changed)

    assert store.get(record.id) == before


def test_checkpoint_order_does_not_reverify_accepted_damaged_evidence(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    for call_id in ("2" * 32, "1" * 32):
        path = directory / f"{call_id}.json"
        path.write_text(f'{{"call":"{call_id}"}}')
        record.task.calls.append(
            ToolCall(
                id=call_id,
                node_id="compute",
                tool="offline_rmse",
                tool_version="1",
                baseline_version=1,
                started_at=now(),
            )
        )
        record.task.checkpoints[call_id] = path.name
        record.task.checkpoint_sha256[call_id] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    store.save(record)
    reloaded = store.get(record.id)
    for path in directory.glob("*.json"):
        path.write_text('{"damaged":true}')
    reloaded.task.stage = "planning"

    store.save(reloaded)

    assert store.get(record.id) == reloaded


def test_persisted_task_directory_identity_is_immutable_without_artifacts(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    store.save(record)
    before = store.get(record.id)
    changed = store.get(record.id)
    output_root = tmp_path / "alternate"
    result_directory = output_root / f"flight-agent-{changed.task.id}"
    result_directory.mkdir(parents=True)
    changed.task.output_root = str(output_root)
    changed.task.result_directory = str(result_directory)

    with pytest.raises(ValueError, match="directory|identity"):
        store.save(changed)

    assert store.get(record.id) == before


def test_codec_rejects_unrecognized_task_state_fields(tmp_path, monkeypatch):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    task_type = type(record.task)
    model_dump = task_type.model_dump

    def dump_with_future_field(task, *args, **kwargs):
        value = model_dump(task, *args, **kwargs)
        value["future_field"] = "unsupported"
        return value

    monkeypatch.setattr(task_type, "model_dump", dump_with_future_field)
    with pytest.raises(ValueError, match="Unsupported task state fields"):
        encode(record)


def test_recent_uses_timestamp_then_id_and_bad_ids_do_not_resolve_paths(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    records = [store.create() for _ in range(22)]
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE conversations SET updated_at='2026-10-31T00:00:00+00:00'")
    assert [item.id for item in store.recent()] == sorted(
        [item.id for item in records]
    )[:20]
    for identifier in ("../outside", "A" * 32, "a" * 31, "a" * 33):
        with pytest.raises(KeyError, match="Unknown conversation"):
            store.get(identifier)


def test_oversize_state_preserves_last_committed_record(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    before = store.get(record.id)
    record.messages.append(
        ConversationMessage(role="user", content="x" * 8_000_000, created_at=now())
    )
    with pytest.raises(ValueError, match="8 MB"):
        store.save(record)
    assert store.get(record.id) == before


def test_restart_interrupts_running_calls_even_under_nonrunning_header(tmp_path):
    from flight_agent.runtime.agent_contracts import InvocationSpec
    from flight_agent.verification.contracts import ToolCall

    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    task = record.task
    task.nodes[0].status = "running"
    call = ToolCall(
        id="1" * 32,
        node_id="compute",
        tool="offline_rmse",
        tool_version="1",
        baseline_version=1,
        started_at=now(),
        inputs={"observed": [0, 2], "reference": [0, 0]},
    )
    task.calls.append(call)
    task.pending_specs[call.id] = InvocationSpec(
        capability="offline_rmse", capability_version="1", inputs=call.inputs
    )
    record.status = "failed"
    store.save(record)
    saved = SQLiteConversationStore(store.root).get(record.id)
    assert saved.status == "interrupted"
    assert saved.task.calls[0].status == "interrupted"
    assert saved.task.calls[0].id == call.id
    assert saved.task.calls[0].inputs == call.inputs
    assert saved.task.pending_specs == task.pending_specs
    assert saved.task.budget_state == task.budget_state
    assert saved.task.nodes[0].status == "interrupted"
    assert SQLiteConversationStore(store.root).get(record.id) == saved


def test_planning_restart_restores_followup_origin_without_touching_baseline(tmp_path):
    from flight_agent.verification.contracts import ConversationState

    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    record.turn_origin = ConversationState(status="waiting_for_user")
    record.status = "planning"
    store.save(record)
    saved = SQLiteConversationStore(store.root).get(record.id)
    assert saved.status == "waiting_for_user"
    assert saved.turn_origin is None
    assert saved.task.baseline == record.task.baseline
    assert saved.messages[-1].role == "assistant"
    assert len(SQLiteConversationStore(store.root).get(record.id).messages) == 1


def test_export_is_stable_complete_and_does_not_change_live_state(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    (directory / "metric.json").write_text('{"rmse":1}')
    record.task.artifacts.append(
        OutputArtifact(**describe_evidence(directory, "metric.json").model_dump())
    )
    store.save(record)
    before = store.get(record.id)
    first = store.export(record.id)
    assert first == store.export(record.id)
    assert first.endswith(b"\n")
    payload = json.loads(first)
    assert payload["schema_version"] == 1
    assert payload["conversation"] == before.model_dump(mode="json")
    assert payload["artifact_index"][0]["relative_path"] == "metric.json"
    assert payload["task_events"][0]["kind"] == "state_transition"
    assert store.get(record.id) == before
    (directory / "metric.json").write_text('{"rmse":2}')
    with pytest.raises(ValueError, match="integrity"):
        store.export(record.id)


def test_planning_restart_with_running_call_restores_origin_and_recovery_evidence(
    tmp_path,
):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    task = record.task
    run = invocation_run()
    persist_agent_run(directory, run)
    task.calls.append(
        ToolCall(
            id=run.id,
            node_id="compute",
            tool=run.spec.capability,
            tool_version=run.spec.capability_version,
            baseline_version=task.baseline.version,
            status="running",
            inputs=run.spec.inputs,
            started_at=run.started_at,
        )
    )
    task.pending_specs[run.id] = run.spec
    task.analysis_runs.append(run)
    task.nodes[0].status = "running"
    checkpoint = directory / "checkpoint.json"
    checkpoint.write_text('{"progress":1}')
    task.checkpoints[run.id] = checkpoint.name
    task.checkpoint_sha256[run.id] = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    metric = directory / "metric.json"
    metric.write_text('{"rmse":1}')
    task.artifacts.append(
        OutputArtifact(**describe_evidence(directory, metric.name).model_dump())
    )
    task.budget_state = BudgetState(
        started_at=now(),
        calls=1,
        rounds=1,
        round_charges=["compute"],
    )
    origin = ConversationState(
        status="waiting_for_user",
        pending_decision=PendingDecision(kind="retry", message="Continue prior task"),
        error="Prior task state",
    )
    record.turn_origin = origin
    record.status = "planning"
    store.save(record)

    saved = SQLiteConversationStore(store.root).get(record.id)

    assert saved.status == origin.status
    assert saved.pending_decision == origin.pending_decision
    assert saved.error == origin.error
    assert saved.turn_origin is None
    assert saved.task.calls[0].status == "interrupted"
    assert saved.task.pending_specs == task.pending_specs
    assert saved.task.analysis_runs == task.analysis_runs
    assert saved.task.checkpoints == task.checkpoints
    assert saved.task.checkpoint_sha256 == task.checkpoint_sha256
    assert saved.task.artifacts == task.artifacts
    assert saved.task.budget_state == task.budget_state
    assert len(saved.messages) == 1
    with sqlite3.connect(store.database_path) as connection:
        event_count = connection.execute("SELECT count(*) FROM task_events").fetchone()[0]

    restarted = SQLiteConversationStore(store.root).get(record.id)

    assert restarted == saved
    assert len(restarted.messages) == 1
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == event_count


@pytest.mark.parametrize("missing_task", ["current", "archived"])
def test_export_validates_artifact_free_current_and_archived_task_directories(
    tmp_path, missing_task
):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    current_directory = store.task_directory(record.id, "a" * 32, create=True)
    archived_directory = store.task_directory(record.id, "b" * 32, create=True)
    record.task = agent_task(current_directory)
    archived = agent_task(archived_directory)
    archived.id = "b" * 32
    for task in (record.task, archived):
        task.baseline = task.baseline.model_copy(update={"confirmed": True, "confirmed_at": now()})
    record.archived_tasks = [archived]
    store.save(record)
    target = current_directory if missing_task == "current" else archived_directory
    target.rmdir()

    with pytest.raises(ValueError, match="directory is missing"):
        store.export(record.id)


@pytest.mark.parametrize("absent", ["current", "archived", "both"])
def test_export_accepts_missing_unconfirmed_proposal_directories(tmp_path, absent):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    current = store.default_task_root(record.id) / ("a" * 32)
    archived = store.default_task_root(record.id) / ("b" * 32)
    record.task = agent_task(current)
    previous = agent_task(archived).model_copy(update={"id": "b" * 32})
    record.archived_tasks = [previous]
    if absent == "current":
        archived.mkdir(parents=True)
    elif absent == "archived":
        current.mkdir(parents=True)
    store.save(record)

    payload = json.loads(store.export(record.id))

    assert payload["conversation"] == store.get(record.id).model_dump(mode="json")
    assert payload["artifact_index"] == []
    assert current.exists() == (absent == "archived")
    assert archived.exists() == (absent == "current")


def test_export_rejects_missing_unconfirmed_directory_with_published_evidence(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    evidence = directory / "metric.json"
    evidence.write_text('{"rmse":1}')
    record.task.artifacts.append(
        OutputArtifact(**describe_evidence(directory, evidence.name).model_dump())
    )
    store.save(record)
    evidence.unlink()
    directory.rmdir()

    with pytest.raises(ValueError, match="directory is missing|evidence"):
        store.export(record.id)


@pytest.mark.parametrize("slot", ["result", "output"])
@pytest.mark.parametrize("missing", [False, True])
def test_export_checks_unindexed_result_and_output_descriptors(tmp_path, slot, missing):
    from flight_agent.runtime.observations import CapabilityOutcome, TaskResult

    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    evidence = directory / "metric.json"
    evidence.write_text('{"rmse":1}')
    descriptor = OutputArtifact(**describe_evidence(directory, evidence.name).model_dump())
    if slot == "result":
        record.task.result = TaskResult(execution_status="partial", artifacts=[descriptor])
    else:
        record.task.outputs["compute"] = CapabilityOutcome(artifacts=[descriptor])
    store.save(record)
    if missing:
        evidence.unlink()
        directory.rmdir()
    else:
        evidence.write_text('{"rmse":2}')

    with pytest.raises(ValueError, match="directory is missing|integrity"):
        store.export(record.id)


def test_export_rejects_mismatched_unconfirmed_proposal_directory(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    expected = store.default_task_root(record.id) / ("a" * 32)
    record.task = agent_task(expected)
    store.save(record)
    with sqlite3.connect(store.database_path) as connection:
        state = json.loads(connection.execute("SELECT state_json FROM tasks").fetchone()[0])
        state["result_directory"] = str(tmp_path / "other")
        connection.execute("UPDATE tasks SET state_json=?", (json.dumps(state),))

    with pytest.raises(ValueError, match="directory|identity"):
        store.export(record.id)


def test_export_rejects_symlink_for_unconfirmed_proposal_directory(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.default_task_root(record.id) / ("a" * 32)
    record.task = agent_task(directory)
    store.save(record)
    directory.parent.mkdir(parents=True)
    directory.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        store.export(record.id)


def test_recovery_reserve_accepts_near_limit_state_that_can_recover(tmp_path):
    hard_limit = 8_000_000
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    record.status = "planning"
    record.turn_origin = ConversationState(status="waiting_for_user")
    created_at = now()
    record.messages.append(
        ConversationMessage(role="user", content="x", created_at=created_at)
    )

    base_size = len(canonical(record.model_dump(mode="json")).encode("utf-8"))
    content = "x" * (1 + hard_limit - 16 - base_size)
    record.messages[0] = ConversationMessage(
        role="user", content=content, created_at=created_at
    )
    assert len(canonical(record.model_dump(mode="json")).encode("utf-8")) == hard_limit - 16
    with pytest.raises(ValueError, match="recovery reserve"):
        store.save(record)

    record.messages[0] = ConversationMessage(
        role="user", content=content[:-1008], created_at=created_at
    )
    accepted_size = len(canonical(record.model_dump(mode="json")).encode("utf-8"))
    assert accepted_size == hard_limit - 1024
    store.save(record)

    recovered = SQLiteConversationStore(store.root).get(record.id)

    assert recovered.status == "waiting_for_user"
    assert recovered.turn_origin is None
    assert len(canonical(recovered.model_dump(mode="json")).encode("utf-8")) <= hard_limit
    assert len(SQLiteConversationStore(store.root).get(record.id).messages) == 2


def test_export_keeps_one_snapshot_when_writer_commits_after_aggregate_read(
    tmp_path, monkeypatch
):
    from flight_agent.verification import sqlite_store as sqlite_store_module

    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    directory = store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    store.save(record)
    aggregate_read = threading.Event()
    writer_finished = threading.Event()
    writer_errors = []
    original_decode = sqlite_store_module.decode

    def coordinated_decode(connection, conversation_id):
        value = original_decode(connection, conversation_id)
        aggregate_read.set()
        assert writer_finished.wait(timeout=5)
        return value

    def write_event():
        try:
            assert aggregate_read.wait(timeout=5)
            with sqlite3.connect(store.database_path, timeout=5) as connection:
                connection.execute(
                    """INSERT INTO task_events(task_id, kind, created_at, payload_json)
                       VALUES (?, 'writer_transition', ?, '{}')""",
                    (record.task.id, now()),
                )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    monkeypatch.setattr(sqlite_store_module, "decode", coordinated_decode)
    writer = threading.Thread(target=write_event)
    writer.start()
    payload = json.loads(store.export(record.id))
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert [event["kind"] for event in payload["task_events"]] == ["state_transition"]
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 2
