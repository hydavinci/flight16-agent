import json
import sqlite3

from flight_agent.verification import sqlite_schema
from flight_agent.verification.contracts import ConversationState
from flight_agent.verification.sqlite_store import SQLiteConversationStore


def test_queued_fields_round_trip_without_opening_a_second_store(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    record.queue_origin = ConversationState(status=record.status)
    record.queued_action = "confirm"
    record.queued_action_committed = True
    record.status = "queued"
    store.save(record)
    restored = store.get(record.id)
    assert restored.status == "queued"
    assert restored.queued_action == "confirm"
    assert restored.queued_action_committed is True
    assert restored.queue_origin == record.queue_origin
    with sqlite3.connect(store.database_path) as connection:
        status, scheduling, payload = connection.execute(
            "SELECT status, scheduling_status, state_json FROM conversations WHERE id=?",
            (record.id,),
        ).fetchone()
    assert status == "running"
    assert scheduling == "queued"
    assert json.loads(payload)["queued_action"] == "confirm"
    assert json.loads(payload)["queued_action_committed"] is True
    record.status = "waiting_for_user"
    record.queued_action = None
    record.queued_action_committed = False
    record.queue_origin = None
    store.save(record)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT scheduling_status FROM conversations WHERE id=?", (record.id,)
        ).fetchone()[0] is None


def test_stage_two_database_upgrades_without_changing_migration_one(
    tmp_path, monkeypatch
):
    root = tmp_path / "state"
    root.mkdir()
    identifier = "d" * 32
    (root / identifier).mkdir()
    path = root / "conversations.sqlite3"
    migrations = sqlite_schema.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "MIGRATIONS", migrations[:1])
        sqlite_schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        digest = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=1"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO conversations
               (id, created_at, updated_at, status, pending_decision_json, state_json)
               VALUES (?, ?, ?, 'waiting_for_user', 'null', ?)""",
            (
                identifier,
                "2026-10-31T00:00:00+00:00",
                "2026-10-31T00:00:00+00:00",
                '{"schema_version":"2.0","output_root_reset":false,"turn_origin":null}',
            ),
        )
    store = SQLiteConversationStore(root)
    restored = store.get(identifier)
    assert restored.status == "waiting_for_user"
    assert restored.queued_action is None
    assert restored.queued_action_committed is False
    assert restored.queue_origin is None
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == migrations[-1].version
        )
        assert (
            connection.execute(
                "SELECT sha256 FROM schema_migrations WHERE version=1"
            ).fetchone()[0]
            == digest
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
