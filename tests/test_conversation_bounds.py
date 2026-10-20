import pytest

from flight_agent.verification.contracts import ConversationMessage
from flight_agent.verification.store import ConversationStore, now


def test_oversized_conversation_cannot_replace_its_last_readable_record(tmp_path):
    store = ConversationStore(tmp_path / "state")
    record = store.create()
    original = store.get(record.id)
    record.messages.append(
        ConversationMessage(role="user", content="x" * 8_000_000, created_at=now())
    )
    with pytest.raises(ValueError, match="8 MB|size|limit"):
        store.save(record)
    assert store.get(record.id) == original
