from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from flight_agent.config import SchedulerSettings
from flight_agent.runtime.budget import BudgetExhausted
from flight_agent.runtime.capabilities import CapabilityDefinition
from flight_agent.runtime.scheduling import (
    CapacityExceeded,
    ExecutionScheduler,
    PlanningScheduler,
    SchedulerClosed,
)
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata


def definition(effect="execute_project"):
    return CapabilityDefinition(
        name="synthetic", version="1", description="Test.", effect=effect
    )


def test_planning_queue_accepts_waiting_work_without_global_busy():
    runner = PlanningScheduler(1, 1)
    entered, release, second = Event(), Event(), Event()

    def first():
        entered.set()
        assert release.wait(5)

    try:
        with runner.reserve() as reservation:
            first_future = reservation.submit(first)
        assert entered.wait(5)
        with runner.reserve() as reservation:
            second_future = reservation.submit(second.set)
        assert not second.is_set()
        with pytest.raises(CapacityExceeded):
            with runner.reserve():
                pytest.fail("over-capacity work was admitted")
        release.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)
    finally:
        release.set()
        runner.close()
    assert second.is_set()
    with pytest.raises(SchedulerClosed):
        with runner.reserve():
            pytest.fail("closed scheduler admitted work")


def test_unused_and_failed_reservations_release_capacity():
    runner = PlanningScheduler(1, 0)
    try:
        with pytest.raises(ValueError, match="invalid request"):
            with runner.reserve():
                raise ValueError("invalid request")
        with runner.reserve() as reservation:
            future = reservation.submit(
                lambda: (_ for _ in ()).throw(RuntimeError("failure"))
            )
        with pytest.raises(RuntimeError, match="failure"):
            future.result(timeout=5)
        with runner.reserve() as reservation:
            reservation.submit(lambda: None).result(timeout=5)
    finally:
        runner.close()
        runner.close()


def test_cancelled_queued_submission_releases_capacity():
    runner = PlanningScheduler(1, 1)
    entered, release, third = Event(), Event(), Event()

    def first():
        entered.set()
        assert release.wait(5)

    try:
        with runner.reserve() as reservation:
            first_future = reservation.submit(first)
        assert entered.wait(5)
        with runner.reserve() as reservation:
            cancelled = reservation.submit(lambda: None)
        assert cancelled.cancel()
        with runner.reserve() as reservation:
            third_future = reservation.submit(third.set)
        release.set()
        first_future.result(timeout=5)
        third_future.result(timeout=5)
    finally:
        release.set()
        runner.close()
    assert third.is_set()


def test_local_wait_does_not_occupy_remote_or_read_only_permits():
    scheduler = ExecutionScheduler(SchedulerSettings())
    attempted, remote, readonly = Event(), Event(), Event()

    def wait_local():
        attempted.set()
        with pytest.raises(BudgetExhausted):
            with scheduler.slot(
                definition(), SchedulingMetadata("local_matlab"), timeout=0
            ):
                pytest.fail("a second local call acquired the default single slot")

    def other(lane, effect, event):
        with scheduler.slot(definition(effect), SchedulingMetadata(lane), timeout=0):
            event.set()

    with scheduler.slot(definition(), SchedulingMetadata("local_matlab")):
        with ThreadPoolExecutor(3) as pool:
            futures = [
                pool.submit(wait_local),
                pool.submit(other, "remote_worker", "execute_project", remote),
                pool.submit(other, "read_only", "read_only", readonly),
            ]
            for future in futures:
                future.result(timeout=5)
        assert attempted.is_set() and remote.is_set() and readonly.is_set()


def test_lane_permit_is_released_after_exception():
    scheduler = ExecutionScheduler(SchedulerSettings())
    with pytest.raises(RuntimeError, match="known failure"):
        with scheduler.slot(definition(), SchedulingMetadata("local_matlab")):
            raise RuntimeError("known failure")
    with scheduler.slot(definition(), SchedulingMetadata("local_matlab"), timeout=0):
        pass
