import json
import sqlite3

import pytest

from flight_agent.runtime.evidence import describe_evidence
from flight_agent.runtime.observations import OutputArtifact
from flight_agent.verification.contracts import ConversationMessage, PendingDecision
from flight_agent.verification.sqlite_store import SQLiteConversationStore
from flight_agent.verification.store import now
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
