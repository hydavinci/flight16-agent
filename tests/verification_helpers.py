import importlib
import json
from pathlib import Path

import pytest


def verification_module(name):
    try:
        return importlib.import_module(f"flight_agent.verification.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("flight_agent.verification"):
            pytest.fail(f"Directory verification {name} has not been implemented")
        raise


def project_tree(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.slx").write_bytes(b"static discovery fixture, not a simulated model")
    (root / "initialize.m").write_text(
        "function initialize\nerror('Discovery must not execute this file');\nend\n",
        encoding="utf-8",
    )
    (root / "data").mkdir()
    (root / "data" / "input.mat").write_bytes(b"registered dependency")
    manifest = {
        "schema_version": "1.0",
        "name": "Prepared test project",
        "backend": "simulink",
        "model": "model.slx",
        "initializer": "initialize.m",
        "initializer_kind": "function",
        "dependencies": ["data/input.mat"],
        "input_signal": {"name": "stick", "unit": "lb"},
        "response_signal": {"name": "response", "unit": "deg/s"},
        "operating_point_mode": "initialized",
        "feedback_sign": 1,
    }
    (root / "flight-agent.project.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def baseline_data(kinds=("step_response",), confirmed=True):
    criteria = []
    if "step_response" in kinds:
        criteria.extend(
            [
                {"metric": "overshoot", "operator": "<=", "threshold": 10.0, "unit": "%"},
                {"metric": "settling_time", "operator": "<=", "threshold": 3.0, "unit": "s"},
            ]
        )
    if "stability_margins" in kinds:
        criteria.extend(
            [
                {"metric": "gain_margin", "operator": ">=", "threshold": 0.0, "unit": "dB"},
                {"metric": "phase_margin", "operator": ">=", "threshold": 0.0, "unit": "deg"},
            ]
        )
    return {
        "version": 1,
        "project_fingerprint": "a" * 64,
        "trim_point": 1,
        "analyses": list(kinds),
        "criteria": criteria,
        "confirmed": confirmed,
    }


def step_result_data(overshoot=5.0, settling=2.0):
    return {
        "run_id": "1" * 32,
        "project_fingerprint": "a" * 64,
        "trim_point": 1,
        "matlab_version": "test fixture",
        "analyses": [
            {
                "kind": "step_response",
                "execution_status": "completed",
                "valid": True,
                "metrics": {
                    "overshoot": {"value": overshoot, "unit": "%"},
                    "settling_time": {"value": settling, "unit": "s"},
                },
                "artifact_paths": ["step-response.mat", "step-response.png"],
            }
        ],
    }


def margin_result_data(gain=6.0, phase=45.0):
    return {
        "run_id": "2" * 32,
        "project_fingerprint": "a" * 64,
        "trim_point": 1,
        "matlab_version": "test fixture",
        "analyses": [
            {
                "kind": "stability_margins",
                "execution_status": "completed",
                "valid": True,
                "metrics": {
                    "gain_margin": {"value": gain, "unit": "dB"},
                    "phase_margin": {"value": phase, "unit": "deg"},
                },
                "artifact_paths": ["stability-margins.mat", "bode.png"],
            }
        ],
    }
