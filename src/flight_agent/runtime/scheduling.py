import math
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import BoundedSemaphore, Condition, Lock

from ..config import SchedulerSettings
from .budget import BudgetExhausted
from .capabilities import CapabilityDefinition
from .scheduling_contracts import SchedulingMetadata, validate_scheduling


class CapacityExceeded(RuntimeError):
    pass


class SchedulerClosed(RuntimeError):
    pass


class Submission:
    def __init__(self, owner: "BoundedRunner") -> None:
        self._owner = owner
        self._submitted = False
        self._released = False

    def _release(self) -> None:
        with self._owner._condition:
            if not self._released:
                self._released = True
                self._owner._in_use -= 1
                self._owner._condition.notify_all()

    def _release_if_cancelled(self, future: Future[None]) -> None:
        if future.cancelled():
            self._release()

    def submit(self, operation: Callable[[], None]) -> Future[None]:
        with self._owner._condition:
            if self._submitted or self._released:
                raise RuntimeError("Submission reservation has already been consumed")
            future = self._owner._pool.submit(self._run, operation)
            future.add_done_callback(self._release_if_cancelled)
            self._submitted = True
            return future

    def _run(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        finally:
            self._release()


class BoundedRunner:
    def __init__(self, workers: int, capacity: int, prefix: str) -> None:
        if workers < 1 or capacity < workers:
            raise ValueError("Runner capacity must cover positive worker count")
        self._condition = Condition()
        self._close_lock = Lock()
        self._in_use = 0
        self._capacity = capacity
        self._closing = False
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=prefix
        )

    @contextmanager
    def reserve(self) -> Iterator[Submission]:
        with self._condition:
            if self._closing:
                raise SchedulerClosed("Scheduler is shutting down")
            if self._in_use >= self._capacity:
                raise CapacityExceeded("Scheduler admission capacity is exhausted")
            self._in_use += 1
        reservation = Submission(self)
        try:
            yield reservation
        finally:
            if not reservation._submitted:
                reservation._release()

    def close(self) -> None:
        with self._close_lock:
            with self._condition:
                self._closing = True
                while self._in_use:
                    self._condition.wait()
            self._pool.shutdown(wait=True, cancel_futures=False)


class PlanningScheduler(BoundedRunner):
    def __init__(self, workers: int, queued: int) -> None:
        if not 1 <= workers <= 32 or not 0 <= queued <= 256:
            raise ValueError("Planning scheduler bounds are invalid")
        super().__init__(workers, workers + queued, "flight-planning")


class ExecutionTurnScheduler(BoundedRunner):
    def __init__(self, turns: int) -> None:
        if not 1 <= turns <= 64:
            raise ValueError("Execution turn bound must be between 1 and 64")
        super().__init__(turns, turns, "flight-execution-turn")


class ExecutionScheduler:
    def __init__(self, settings: SchedulerSettings) -> None:
        self.settings = settings
        self._slots = {
            "local_matlab": BoundedSemaphore(settings.local_matlab_slots),
            "remote_worker": BoundedSemaphore(settings.remote_worker_slots),
            "read_only": BoundedSemaphore(settings.read_only_slots),
        }

    @contextmanager
    def slot(
        self,
        definition: CapabilityDefinition,
        metadata: SchedulingMetadata,
        *,
        timeout: float | None = None,
    ) -> Iterator[None]:
        declared = validate_scheduling(definition.effect, metadata)
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Lane timeout must be finite and nonnegative")
        semaphore = self._slots[declared.lane]
        acquired = (
            semaphore.acquire()
            if timeout is None
            else semaphore.acquire(timeout=timeout)
        )
        if not acquired:
            raise BudgetExhausted(
                "The confirmed elapsed-time budget expired waiting for a lane"
            )
        try:
            yield
        finally:
            semaphore.release()
