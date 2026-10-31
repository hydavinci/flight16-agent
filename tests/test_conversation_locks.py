from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from flight_agent.runtime.conversation_locks import ConversationLockRegistry

A, B = "a" * 32, "b" * 32


def test_different_conversations_overlap():
    locks = ConversationLockRegistry()
    barrier = Barrier(2)

    def run(identifier):
        with locks.hold(identifier):
            barrier.wait(timeout=5)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, identifier) for identifier in (A, B)]
        for future in futures:
            future.result(timeout=5)
    assert locks.entry_count == 0


def test_same_conversation_waiter_keeps_entry_alive():
    locks = ConversationLockRegistry()
    attempted, entered = Event(), Event()

    def waiter():
        attempted.set()
        with locks.hold(A):
            entered.set()

    with ThreadPoolExecutor(1) as pool:
        with locks.hold(A):
            future = pool.submit(waiter)
            assert attempted.wait(5)
            assert not entered.is_set()
            assert locks.entry_count == 1
            with locks.hold(A):
                assert locks.entry_count == 1
        future.result(timeout=5)
    assert entered.is_set()
    assert locks.entry_count == 0


def test_exception_and_repeated_conversations_release_entries():
    locks = ConversationLockRegistry()
    for number in range(1000):
        with pytest.raises(RuntimeError, match="failed turn"):
            with locks.hold(f"{number:032x}"):
                raise RuntimeError("failed turn")
    assert locks.entry_count == 0


@pytest.mark.parametrize("identifier", ["../task", "/tmp/project", "A" * 32, "", "a" * 31])
def test_invalid_identity_is_rejected_without_allocating(identifier):
    locks = ConversationLockRegistry()
    with pytest.raises(ValueError, match="conversation ID"):
        with locks.hold(identifier):
            pytest.fail("invalid identity acquired a lock")
    assert locks.entry_count == 0
