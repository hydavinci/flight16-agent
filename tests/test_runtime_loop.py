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


def test_generic_execution_obeys_the_shared_progression_bound(tmp_path, monkeypatch):
    from tests.agent_helpers import analysis_plan_data
    from tests.test_agent_runtime import confirm, propose, runtime_module, setup

    loop = kernel("loop")

    def limited(**options):
        return loop.run_loop(**{**options, "max_steps": 1})

    monkeypatch.setattr(runtime_module(), "run_loop", limited)
    runtime, _, conversation, calls = setup(tmp_path)
    data = analysis_plan_data()
    data["nodes"].append({**data["nodes"][0], "id": "second-analysis"})
    done = confirm(runtime, propose(runtime, conversation, data))
    assert done.status == "failed"
    assert "step budget" in done.error.lower()
    assert len(calls) == 1
    assert len(done.task.analysis_runs) == 1
    assert done.task.nodes[1].status == "pending"
