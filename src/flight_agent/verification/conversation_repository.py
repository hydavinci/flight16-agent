from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .contracts import Conversation


class ConversationRepository(Protocol):
    root: Path

    def create(self) -> Conversation: ...

    def get(self, conversation_id: str) -> Conversation: ...

    def save(self, record: Conversation) -> None: ...

    def recent(self) -> list[Conversation]: ...

    def export(self, conversation_id: str) -> bytes: ...

    def directory(self, conversation_id: str) -> Path: ...

    def default_task_root(self, conversation_id: str) -> Path: ...

    def task_directory(
        self,
        conversation_id: str,
        task_id: str,
        output_root: str | None = None,
        *,
        create: bool = False,
    ) -> Path: ...

    def transaction(self) -> AbstractContextManager[None]: ...
