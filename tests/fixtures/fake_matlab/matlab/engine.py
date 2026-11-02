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
    def __init__(self):
        self.directory = Path.cwd()
        self.paths = []

    def cd(self, path, **kwargs):
        if not Path(path).is_dir():
            raise MatlabExecutionError("entry missing")
        self.directory = Path(path)

    def addpath(self, path, **kwargs):
        if not Path(path).is_dir():
            raise MatlabExecutionError("path missing")
        if Path(path).name != "resources":
            self.paths.append(Path(path))

    def feval(self, name, payload, *args, **kwargs):
        if name == "flight_agent_verify":
            return self.verification(payload)
        raise MatlabExecutionError("unapproved function")

    def verification(self, directory):
        from PIL import Image

        directory = Path(directory)
        request = json.loads((directory / "request.json").read_text())
        project = json.loads((directory / "project.json").read_text())
        assert (directory / "project" / project["config"]["model"]).is_file()
        assert "LLM_API_KEY" not in os.environ
        mode = os.environ.get("TEST_ENGINE_MODE", "")
        if mode == "execution_error":
            raise MatlabExecutionError("test solver failed")
        if mode == "verification_block":
            time.sleep(60)
        output = {
            "run_id": request["id"],
            "project_fingerprint": request["project_fingerprint"],
            "trim_point": request["trim_point"],
            "matlab_version": "test-engine",
            "analyses": [],
        }
        if mode == "verification_wrong_context":
            output["trim_point"] = 2
        for kind in request["analyses"]:
            if kind == "step_response":
                metrics = {
                    "overshoot": {"value": 5.0, "unit": "%"},
                    "settling_time": {"value": 2.0, "unit": "s"},
                }
                names = ["step-response.mat", "step-response.png"]
            else:
                metrics = {
                    "gain_margin": {"value": 6.0, "unit": "dB"},
                    "phase_margin": {"value": 45.0, "unit": "deg"},
                }
                names = ["stability-margins.mat", "bode.png"]
            output["analyses"].append(
                {
                    "kind": kind,
                    "valid": True,
                    "metrics": metrics,
                    "artifact_paths": names,
                }
            )
            (directory / names[0]).write_bytes(b"MATLAB 5.0 MAT-file, test transport fixture")
            Image.new("RGB", (2, 2), color="blue").save(directory / names[1])
            if mode == "verification_missing_artifact":
                (directory / names[0]).unlink()
            if mode == "verification_bad_image":
                (directory / names[1]).write_bytes(b"not PNG")
        if mode == "verification_missing_branch":
            output["analyses"] = []
        (directory / "analysis.json").write_text(json.dumps(output))

    def quit(self):
        marker = os.environ.get("TEST_QUIT_FILE")
        if marker:
            Path(marker).write_text("quit")
