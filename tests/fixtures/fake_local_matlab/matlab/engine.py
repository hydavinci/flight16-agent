import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image


class EngineError(RuntimeError):
    pass


class MatlabExecutionError(RuntimeError):
    pass


def mode():
    return os.environ.get("TEST_LOCAL_MODE", "")


if mode() in {"probe_block", "probe_flood"}:
    if mode() == "probe_flood":
        sys.stdout.write("x" * 100_000)
        sys.stdout.flush()
    time.sleep(60)


def increment(path):
    path.write_text(str(int(path.read_text()) + 1) if path.exists() else "1")


def start_matlab():
    increment(Path("engine-starts.txt"))
    if os.environ.get("TEST_LOCAL_STARTS"):
        increment(Path(os.environ["TEST_LOCAL_STARTS"]))
    assert not any(
        key.startswith(("LLM_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "MATLAB_WORKER_"))
        for key in os.environ
    ), "Credentials leaked into owned worker"
    state = json.loads(Path("state.json").read_text())
    assert state["status"] == "running" and state["request_sha256"]
    if mode() == "startup_error":
        raise EngineError("license unavailable")
    return Engine()


class Engine:
    def cd(self, path, *, nargout):
        assert nargout == 0
        self.support = Path(path)

    def addpath(self, path, *, nargout):
        assert nargout == 0 and Path(path) == self.support

    def which(self, name, *, nargout):
        assert nargout == 1 and name in {"flight_agent_run_project", "flight_agent_local_stepinfo"}
        if mode() == "helper_shadow":
            return "/another/flight_agent_run_project.m"
        return str(self.support / (name + ".m"))

    def feval(self, name, request_path, *, nargout):
        assert nargout == 0
        if name == "flight_agent_local_stepinfo":
            self.stepinfo(request_path)
            return
        assert name == "flight_agent_run_project"
        execution = json.loads(Path(request_path).read_text())
        canonical = json.loads(Path("request.json").read_text())
        assert execution["id"] == canonical["id"]
        assert execution["inputs"] == canonical["inputs"]
        root = Path(execution["snapshot_root"])
        assert root == Path.cwd() / "project"
        assert (root / "initialize.m").is_file()
        if canonical["inputs"]["mode"] == "simulink":
            assert (root / canonical["inputs"]["model"]).is_file()
        Path("calls.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "request_path": request_path,
                    "nargout": nargout,
                    "snapshot_root": str(root),
                    "inputs": execution["inputs"],
                }
            )
        )
        if mode() == "execution_error":
            raise MatlabExecutionError("solver failed")
        if mode() == "function_shadow":
            raise MatlabExecutionError("Selected function resolution mismatch")
        if mode() in {"block", "artifact_flood", "log_flood"}:
            if mode() == "block":
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                        " time.sleep(60)",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                Path("descendant.pid").write_text(str(child.pid))
            elif mode() == "artifact_flood":
                with Path("raw.mat").open("wb") as stream:
                    stream.truncate(101 * 1024 * 1024)
            else:
                sys.stdout.write("x" * 4_100_000)
                sys.stdout.flush()
            time.sleep(60)
        if mode() == "missing_receipt":
            os._exit(0)
        if mode() == "malformed":
            Path("bridge.json").write_text("not JSON")
            return
        if mode() == "oversized":
            Path("bridge.json").write_text("x" * 4_100_000)
            return
        Path("raw.mat").write_bytes(b"MATLAB 5.0 MAT-file, test-only actual-result placeholder")
        runtime = {
            "matlab_version": "test-only-engine",
            "matlab_release": "test-only",
            "id": execution["id"],
            "request_sha256": execution["request_sha256"],
            "snapshot_root": execution["snapshot_root"],
            "working_directory": execution["snapshot_root"],
            "products": [{"Name": "test-only", "Version": "test-only"}],
            "settings": {"StopTime": "12", "Solver": "ode45"},
        }
        if mode() == "wrong_runtime_id":
            runtime["id"] = "b" * 32
        if mode() == "wrong_runtime_digest":
            runtime["request_sha256"] = "b" * 64
        if mode() == "contradictory_runtime_inputs":
            runtime["inputs"] = {"model": []}
        Path("runtime-metadata.json").write_text(json.dumps(runtime))
        if mode() == "bad_metadata":
            Path("runtime-metadata.json").write_text("{}")
        paths = ["raw.mat", "runtime-metadata.json"]
        evidence = True
        if mode() == "no_evidence":
            evidence = False
        if mode() not in {"raw_only", "return_only", "no_series"}:
            series = {
                "input": {
                    "time": [0, 1, 2],
                    "values": [0, 1, 1],
                    "time_unit": "s",
                    "unit": "deg",
                    "name": "command",
                },
                "response": {
                    "time": [0, 1, 2],
                    "values": [0, 2, 3],
                    "time_unit": "s",
                    "unit": "deg/s",
                    "name": "response",
                },
            }
            if mode() == "bad_series":
                series["response"]["values"] = [1]
            if mode() == "wrong_unit":
                series["response"]["time_unit"] = "fortnights"
            if mode() == "wrong_signal":
                series["response"]["name"] = "unrequested"
            if mode() == "milliseconds":
                series["response"]["time_unit"] = "milliseconds"
            Path("series.json").write_text(json.dumps(series))
            paths.append("series.json")
        if mode() == "return_only":
            Path("return.json").write_text(json.dumps({"result": [1, 2, 3]}))
            paths.append("return.json")
        if mode() == "missing_artifact":
            Path("raw.mat").unlink()
        if mode() == "artifact_symlink":
            Path("raw.mat").unlink()
            Path("raw.mat").symlink_to(root / "data" / "trim.mat")
        Path("bridge.json").write_text(
            json.dumps(
                {
                    "id": "b" * 32 if mode() == "wrong_identity" else execution["id"],
                    "request_sha256": execution["request_sha256"],
                    "artifact_paths": paths,
                    "output_evidence": evidence,
                    "details": {"mode": canonical["inputs"]["mode"]},
                    "diagnostics": [],
                }
            )
        )
        print("test MATLAB execution output")

    def stepinfo(self, request_path):
        execution = json.loads(Path(request_path).read_text())
        canonical = json.loads(Path("request.json").read_text())
        assert canonical["operation"] == "stepinfo"
        assert execution["inputs"] == canonical["inputs"]
        assert execution["series_path"] == str(Path.cwd() / "project" / "series.json")
        assert canonical["input_evidence"] == [canonical["inputs"]["series"]]
        if mode() == "execution_error":
            raise MatlabExecutionError("test-only metric operation failed")
        if mode() == "missing_receipt":
            os._exit(0)
        Path("series.json").write_bytes(Path(execution["series_path"]).read_bytes())
        Path("raw.mat").write_bytes(b"MATLAB 5.0 MAT-file, test-only metric placeholder")
        Image.new("RGB", (8, 8), "white").save("step-response.png")
        runtime = {
            "id": execution["id"],
            "request_sha256": execution["request_sha256"],
            "snapshot_root": execution["snapshot_root"],
            "working_directory": str(Path.cwd()),
            "matlab_version": "test-only-engine",
            "matlab_release": "test-only",
            "products": [{"Name": "test-only", "Version": "test-only"}],
            "settings": {},
        }
        if mode() == "wrong_runtime_id":
            runtime["id"] = "0" * 32
        if mode() == "contradictory_runtime_inputs":
            runtime["inputs"] = canonical["inputs"]
        Path("runtime-metadata.json").write_text(json.dumps(runtime))
        valid = mode() != "invalid_step"
        metrics = (
            {
                "overshoot": {"value": 12.5, "unit": "%"},
                "settling_time": {"value": 3.25, "unit": "s"},
            }
            if valid
            else {}
        )
        details = {
            "mode": "stepinfo",
            "settling_band": execution["inputs"]["settling_band"],
            "steady_state_policy": execution["inputs"]["steady_state"],
        }
        if mode() == "bad_band":
            details["settling_band"] = 0.9
        series = json.loads(Path("series.json").read_text())
        for role in ("input", "response"):
            details[role + "_signal"] = {key: series[role][key] for key in ("name", "unit")}
        if mode() == "wrong_step_signal":
            details["response_signal"]["unit"] = "invented"
        if mode() == "wrong_metric_unit":
            metrics["settling_time"]["unit"] = "ms"
        if mode() == "changed_step_source":
            artifact = canonical["inputs"]["series"]
            (Path.cwd().parent / artifact["path"]).write_text("{}")
        if mode() == "missing_plot":
            Path("step-response.png").unlink()
        Path("bridge.json").write_text(
            json.dumps(
                {
                    "id": execution["id"],
                    "request_sha256": execution["request_sha256"],
                    "artifact_paths": [
                        "raw.mat",
                        "runtime-metadata.json",
                        "series.json",
                        "step-response.png",
                    ],
                    "output_evidence": True,
                    "valid": valid,
                    "metrics": metrics,
                    "details": details,
                    "diagnostics": [] if valid else ["Unreliable steady state in test-only series"],
                }
            )
        )

    def quit(self):
        increment(Path("engine-quits.txt"))
        if mode() == "quit_error":
            raise EngineError("Engine cleanup failed")
