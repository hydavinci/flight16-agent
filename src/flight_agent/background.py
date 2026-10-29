import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor


class BusyError(ValueError):
    pass


class BackgroundRunner:
    def __init__(self, max_workers: int = 1):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="flight-agent-task",
        )
        self.slot = threading.BoundedSemaphore(max_workers)

    def submit(self, operation: Callable[[], None]) -> None:
        if not self.slot.acquire(blocking=False):
            raise BusyError("Another task is running; wait for it to finish")
        try:
            self.pool.submit(self._run, operation)
        except RuntimeError:
            self.slot.release()
            raise

    def _run(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        finally:
            self.slot.release()

    def close(self) -> None:
        self.pool.shutdown(wait=True)
