import pytest

from tests.test_runtime_kernel import kernel, scope


def test_next_decision_observes_persisted_output_before_dispatching_again():
    loop = kernel("loop")
    state = []
    saved = []

    def decide():
        assert saved == state
        if state == [2, 5]:
            return loop.LoopDecision(action="stop", reason="Target observed")
        return loop.LoopDecision(action="continue", reason="Need another observation")

    def execute():
        state.append(2 if not state else state[-1] + 3)

    result = loop.run_loop(
        decide=decide, execute=execute, persist=lambda: saved.extend(state[len(saved):]),
        scope=scope(), max_steps=2,
    )
    assert state == [2, 5]
    assert saved == [2, 5]
    assert result.action == "stop"


@pytest.mark.parametrize("action", ["stop", "clarify"])
def test_stop_and_clarification_return_without_dispatch(action):
    loop = kernel("loop")
    dispatched = []
    result = loop.run_loop(
        decide=lambda: loop.LoopDecision(action=action, reason="More input is required"),
        execute=lambda: dispatched.append("executed"),
        persist=lambda: None, scope=scope(confirmed=False), max_steps=1,
    )
    assert result.action == action
    assert not dispatched


@pytest.mark.parametrize(
    "changes,effect,error",
    [({"confirmed": False}, "read_only", "confirm"),
     ({}, "execute_project", "permission")],
)
def test_shared_driver_enforces_confirmation_and_effects(changes, effect, error):
    loop = kernel("loop")
    dispatched = []
    with pytest.raises(ValueError, match=error):
        loop.run_loop(
            decide=lambda: loop.LoopDecision(action="continue", effect=effect, reason="Run"),
            execute=lambda: dispatched.append("executed"),
            persist=lambda: None, scope=scope(**changes), max_steps=2,
        )
    assert not dispatched


def test_step_budget_blocks_an_unbounded_policy_before_an_extra_dispatch():
    loop = kernel("loop")
    dispatched = []
    with pytest.raises(ValueError, match="budget|step"):
        loop.run_loop(
            decide=lambda: loop.LoopDecision(action="continue", reason="Again"),
            execute=lambda: dispatched.append("executed"),
            persist=lambda: None, scope=scope(), max_steps=2,
        )
    assert dispatched == ["executed", "executed"]


def test_failure_is_propagated_without_retry_or_successful_persistence():
    loop = kernel("loop")
    dispatched = []
    saved = []

    def fail():
        dispatched.append("executed")
        raise OSError("Controlled failure")

    with pytest.raises(OSError, match="Controlled failure"):
        loop.run_loop(
            decide=lambda: loop.LoopDecision(action="continue", reason="Run"),
            execute=fail, persist=lambda: saved.append("saved"), scope=scope(), max_steps=2,
        )
    assert dispatched == ["executed"]
    assert not saved


@pytest.mark.parametrize("scenario", ["d1", "d2"])
def test_domain_execution_obeys_the_shared_progression_bound(tmp_path, monkeypatch, scenario):
    from tests.verification_helpers import verification_module

    loop = kernel("loop")

    def limited(**options):
        return loop.run_loop(**{**options, "max_steps": 1})

    module = verification_module("runtime" if scenario == "d1" else "diagnosis")
    monkeypatch.setattr(module, "run_loop", limited, raising=False)
    if scenario == "d1":
        from tests.test_verification_runtime import proposal, runtime_fixture

        runtime, _, _, root = runtime_fixture(tmp_path, [proposal()])
    else:
        from tests.test_diagnosis_runtime import runtime_fixture

        runtime, _, _, root = runtime_fixture(tmp_path)
    pending = runtime.submit(None, "Analyze the synthetic evidence.", str(root), scenario=scenario)
    done = runtime.confirm(pending.id, pending.task.baseline.version, trusted=True)
    assert done.status == "failed"
    assert "step budget" in done.error.lower()
    if scenario == "d1":
        assert done.task.evaluation is None
    else:
        assert len(done.task.rounds) == 1
        assert len(done.task.analysis_runs) == 1
