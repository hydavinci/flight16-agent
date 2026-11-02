import importlib
import json
from pathlib import Path

import pytest
from PIL import Image


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


class ControlledBackend:
    def __init__(self):
        self.calls = []
        self.fail_after_completion = False
        self.completed = {}

    def inspect(self, root):
        return verification_module("projects").ProjectInspector().inspect(root)

    def execute(self, request, directory, on_progress, *, resume=False):
        from flight_agent.remote import RemoteError

        contracts = verification_module("contracts")
        self.calls.append((request, resume))
        if resume:
            assert request.id in self.completed
        data = step_result_data()
        data["analyses"] = []
        for kind in request.analyses:
            item = step_result_data() if kind == "step_response" else margin_result_data()
            data["analyses"].extend(item["analyses"])
        directory.mkdir(parents=True, exist_ok=True)
        names = [name for item in data["analyses"] for name in item["artifact_paths"]]
        for name in names:
            path = directory / name
            if path.suffix == ".png":
                Image.new("RGB", (32, 32)).save(path)
            else:
                path.write_bytes(b"MATLAB test fixture" + b" " * 128)
        data.update(
            run_id=request.id,
            project_fingerprint=request.project_fingerprint,
            trim_point=request.trim_point,
            artifacts=[
                verification_module("artifacts").describe_artifact(directory, name).model_dump()
                for name in names
            ],
        )
        result = contracts.AnalysisResult.model_validate(data)
        (directory / "analysis.json").write_text(result.model_dump_json())
        self.completed[request.id] = result
        if self.fail_after_completion and not resume:
            raise RemoteError("Controlled lost acknowledgement")
        store = verification_module("store")
        on_progress(
            contracts.RunRecord(
                id=request.id,
                request_sha256=store.request_digest(request),
                created_at=store.now(),
                updated_at=store.now(),
                status="completed",
                stage="completed",
                result=result,
            )
        )
        return result
