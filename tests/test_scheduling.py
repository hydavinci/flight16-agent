from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from threading import Barrier, Event, Thread

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


def _assert_runner_closed(runner):
    try:
        with runner.reserve():
            raise AssertionError("closed scheduler admitted work")
    except SchedulerClosed:
        pass


def _close_from_operation(completed):
    runner = PlanningScheduler(1, 0)

    def operation():
        runner.close()
        completed.set()

    with runner.reserve() as reservation:
        future = reservation.submit(operation)
    future.result(timeout=5)
    _assert_runner_closed(runner)
    runner.close()


def _close_from_completion_callback(completed):
    runner = PlanningScheduler(1, 0)
    entered, release = Event(), Event()

    def operation():
        entered.set()
        assert release.wait(5)

    def close_runner(_future):
        runner.close()
        completed.set()

    with runner.reserve() as reservation:
        future = reservation.submit(operation)
    assert entered.wait(5)
    future.add_done_callback(close_runner)
    release.set()
    future.result(timeout=5)
    assert completed.wait(5)
    _assert_runner_closed(runner)
    runner.close()


def _assert_process_completes(target, message):
    context = get_context("spawn")
    completed = context.Event()
    process = context.Process(target=target, args=(completed,))
    process.start()
    try:
        assert completed.wait(5), message
        process.join(5)
        assert not process.is_alive(), "scheduler child process did not exit"
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


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


def test_close_from_operation_stops_admission_without_self_deadlock():
    _assert_process_completes(
        _close_from_operation,
        "close() did not return when invoked by an admitted operation",
    )


def test_close_from_completion_callback_stops_admission_without_self_join():
    _assert_process_completes(
        _close_from_completion_callback,
        "close() did not return when invoked by a completion callback",
    )


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


def test_concurrent_close_submit_and_cancel_drains_admitted_work_once():
    runner = PlanningScheduler(1, 2)
    entered, release, third = Event(), Event(), Event()
    close_barrier = Barrier(3)
    closed = (Event(), Event())

    def first():
        entered.set()
        release.wait()

    def close_runner(done):
        close_barrier.wait()
        runner.close()
        done.set()

    try:
        with runner.reserve() as reservation:
            first_future = reservation.submit(first)
        assert entered.wait(5)
        with runner.reserve() as reservation:
            cancelled = reservation.submit(lambda: None)
        with runner.reserve() as delayed:
            threads = [
                Thread(target=close_runner, args=(done,), daemon=True)
                for done in closed
            ]
            for thread in threads:
                thread.start()
            close_barrier.wait()
            with runner._condition:
                assert runner._condition.wait_for(lambda: runner._closing, timeout=5)
            assert cancelled.cancel()
            third_future = delayed.submit(third.set)
            with pytest.raises(SchedulerClosed):
                with runner.reserve():
                    pytest.fail("stopped scheduler admitted work")
    finally:
        release.set()
    first_future.result(timeout=5)
    third_future.result(timeout=5)
    for done in closed:
        assert done.wait(5)
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    with runner._condition:
        assert runner._in_use == 0
    assert third.is_set()


def test_shutdown_drains_running_and_queued_work_but_refuses_new_admission():
    runner = PlanningScheduler(1, 1)
    entered, release, stopped, second = Event(), Event(), Event(), Event()

    def first():
        entered.set()
        assert release.wait(5)

    def close():
        runner.stop_accepting()
        stopped.set()
        runner.close()

    with ThreadPoolExecutor(1) as pool:
        with runner.reserve() as reservation:
            a = reservation.submit(first)
        assert entered.wait(5)
        with runner.reserve() as reservation:
            b = reservation.submit(second.set)
        closing = pool.submit(close)
        try:
            assert stopped.wait(5)
            assert not closing.done()
            with pytest.raises(SchedulerClosed):
                with runner.reserve():
                    pytest.fail("shutdown admitted work")
        finally:
            release.set()
        closing.result(timeout=5)
        a.result(timeout=5)
        b.result(timeout=5)
    assert second.is_set()


def test_shutdown_waits_for_a_reserved_but_not_yet_published_request():
    runner = PlanningScheduler(1, 0)
    stopped = Event()

    def close():
        runner.stop_accepting()
        stopped.set()
        runner.close()

    with ThreadPoolExecutor(1) as pool:
        with runner.reserve() as reservation:
            closing = pool.submit(close)
            assert stopped.wait(5)
            assert not closing.done()
            future = reservation.submit(lambda: None)
        future.result(timeout=5)
        closing.result(timeout=5)


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
