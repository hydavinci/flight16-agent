import json
import os
import subprocess
import sys
import time
from pathlib import Path


class EngineError(Exception):
    pass


class MatlabExecutionError(Exception):
    pass


def start_matlab():
    if os.environ.get("TEST_ENGINE_MODE") == "startup_error":
        raise EngineError("test license unavailable")
    if os.environ.get("TEST_ENGINE_MODE") == "crash_with_child":
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parents[2] / "stubborn_child.py")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        marker = Path(os.environ["TEST_CHILD_PID_FILE"])
        deadline = time.monotonic() + 5
        while not marker.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise EngineError("Test descendant did not become ready")
            time.sleep(0.01)
        os._exit(7)
    return Engine()


class Engine:
    def cd(self, path, **kwargs):
        if not Path(path).is_dir():
            raise MatlabExecutionError("entry missing")

    def addpath(self, path, **kwargs):
        if not Path(path).is_dir():
            raise MatlabExecutionError("path missing")

    def feval(self, name, payload, *args, **kwargs):
        if name == "flight_agent_execute_upload":
            return self.upload(payload, *args, **kwargs)
        if name != "analyze_demo":
            raise MatlabExecutionError("unapproved function")
        mode = os.environ.get("TEST_ENGINE_MODE", "success")
        if mode == "execution_error":
            raise MatlabExecutionError("test solver failed")
        if mode == "block":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            Path(os.environ["TEST_CHILD_PID_FILE"]).write_text(str(child.pid))
            time.sleep(60)
        if mode == "stubborn":
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parents[2] / "stubborn_child.py")],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(60)
        if mode == "malformed":
            return "not json"
        if mode == "wrong_type":
            return 42
        params = json.loads(payload)
        weight = params["mass_kg"] * 9.80665
        pressure = 0.5 * params["air_density_kg_m3"] * params["airspeed_mps"] ** 2
        result = {
            "schema_version": "1.0",
            "aircraft_type": params["aircraft_type"],
            "condition": params["condition"],
            "model_version": "demo-static-v1",
            "matlab_version": "test-engine",
            "valid": True,
            "diagnostics": [],
            "metrics": {
                "wing_loading": {"value": weight / params["wing_area_m2"], "unit": "N/m^2"},
                "dynamic_pressure": {"value": pressure, "unit": "Pa"},
                "required_lift_coefficient": {
                    "value": weight / (pressure * params["wing_area_m2"]),
                    "unit": "1",
                },
                "thrust_to_weight": {"value": params["thrust_n"] / weight, "unit": "1"},
            },
        }
        if mode == "nan":
            result["metrics"]["wing_loading"]["value"] = float("nan")
        if mode == "mismatch":
            result["condition"] = "another-case"
        kwargs["stdout"].write("test MATLAB output\n")
        return json.dumps(result)

    def upload(self, directory, filename, kind, params_json, max_figures, **kwargs):
        from PIL import Image

        directory = Path(directory)
        assert (directory / "code" / filename).is_file()
        assert kind in ("script", "function")
        mode = os.environ.get("TEST_ENGINE_MODE", "")
        if mode == "execution_error":
            raise MatlabExecutionError("test solver failed")
        if mode == "upload_block":
            time.sleep(60)
        if mode == "upload_missing":
            raise MatlabExecutionError("Script must assign a result variable")
        params = json.loads(params_json)
        result = {"value": params.get("value", 3) * 2}
        if mode == "upload_nan":
            result = {"value": float("nan")}
        if mode == "upload_large":
            result = {"value": "x" * 4_000_001}
        if mode == "upload_metrics":
            result = json.loads(os.environ["TEST_METRICS_JSON"])
        Image.new("RGB", (2, 2), color="blue").save(directory / "figure-1.png")
        if mode == "upload_bad_image":
            (directory / "figure-1.png").write_bytes(b"not PNG")
        output = {"result": result, "matlab_version": "test-engine", "figures": ["figure-1.png"]}
        (directory / "output.json").write_text(json.dumps(output))
        kwargs.get("stdout", sys.stdout).write("upload log\n")

    def quit(self):
        marker = os.environ.get("TEST_QUIT_FILE")
        if marker:
            Path(marker).write_text("quit")
