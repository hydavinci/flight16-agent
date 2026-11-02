from dataclasses import replace

import pytest

from flight_agent.config import SchedulerSettings

VARIABLES = {
    "FLIGHT_PLANNING_WORKERS": ("planning_workers", 4, 1, 32),
    "FLIGHT_PLANNING_QUEUE": ("planning_queue", 32, 0, 256),
    "FLIGHT_LOCAL_MATLAB_SLOTS": ("local_matlab_slots", 1, 1, 8),
    "FLIGHT_REMOTE_WORKER_SLOTS": ("remote_worker_slots", 1, 1, 16),
    "FLIGHT_READ_ONLY_SLOTS": ("read_only_slots", 4, 1, 32),
    "FLIGHT_EXECUTION_TURNS": ("execution_turns", 32, 1, 64),
}


def clean(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_defaults_preserve_four_planners_and_one_local_matlab(monkeypatch):
    clean(monkeypatch)
    settings = SchedulerSettings.from_environment()
    for field, default, lower, upper in VARIABLES.values():
        assert getattr(settings, field) == default


@pytest.mark.parametrize("variable", list(VARIABLES))
@pytest.mark.parametrize("value", ["", " ", "0.5", "inf", "NaN", "-1", "+2", "999999"])
def test_invalid_environment_is_not_silently_clamped(monkeypatch, variable, value):
    clean(monkeypatch)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        SchedulerSettings.from_environment()


@pytest.mark.parametrize("variable", list(VARIABLES))
def test_both_inclusive_bounds_are_accepted(monkeypatch, variable):
    clean(monkeypatch)
    monkeypatch.setenv("FLIGHT_EXECUTION_TURNS", "64")
    field, default, lower, upper = VARIABLES[variable]
    for value in (lower, upper):
        if variable == "FLIGHT_EXECUTION_TURNS" and value == 1:
            for name in (
                "FLIGHT_LOCAL_MATLAB_SLOTS",
                "FLIGHT_REMOTE_WORKER_SLOTS",
                "FLIGHT_READ_ONLY_SLOTS",
            ):
                monkeypatch.setenv(name, "1")
            with pytest.raises(ValueError, match="FLIGHT_EXECUTION_TURNS"):
                monkeypatch.setenv(variable, "1")
                SchedulerSettings.from_environment()
            continue
        monkeypatch.setenv(variable, f" {value} ")
        assert getattr(SchedulerSettings.from_environment(), field) == value


def test_direct_settings_construction_cannot_bypass_bounds():
    with pytest.raises(ValueError, match="FLIGHT_LOCAL_MATLAB_SLOTS"):
        replace(SchedulerSettings(), local_matlab_slots=0)
    with pytest.raises(ValueError, match="FLIGHT_EXECUTION_TURNS"):
        replace(SchedulerSettings(), execution_turns=2)
