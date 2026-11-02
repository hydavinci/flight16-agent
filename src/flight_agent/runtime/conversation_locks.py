from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock

from ..models import JOB_ID


@dataclass
class _Entry:
    lock: RLock = field(default_factory=RLock)
    references: int = 0


class ConversationLockRegistry:
    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def entry_count(self) -> int:
        with self._guard:
            return len(self._entries)

    @contextmanager
    def hold(self, conversation_id: str) -> Iterator[None]:
        if not isinstance(conversation_id, str) or not JOB_ID.fullmatch(conversation_id):
            raise ValueError("Invalid conversation ID")
        with self._guard:
            entry = self._entries.setdefault(conversation_id, _Entry())
            entry.references += 1
        acquired = False
        try:
            entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._guard:
                entry.references -= 1
                if entry.references == 0:
                    del self._entries[conversation_id]
