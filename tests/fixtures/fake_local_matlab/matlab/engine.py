import json
import os
import subprocess
import sys
import time
from pathlib import Path


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
        assert nargout == 1 and name == "flight_agent_run_project"
        if mode() == "helper_shadow":
            return "/another/flight_agent_run_project.m"
        return str(self.support / (name + ".m"))

    def feval(self, name, request_path, *, nargout):
        assert name == "flight_agent_run_project" and nargout == 0
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
        Path("metadata.json").write_text(
            json.dumps(
                {
                    "matlab_version": "test-only-engine",
                    "matlab_release": "test-only",
                    "id": execution["id"],
                    "request_sha256": execution["request_sha256"],
                    "snapshot_root": execution["snapshot_root"],
                    "working_directory": execution["snapshot_root"],
                    "inputs": execution["inputs"],
                    "conditions": execution["inputs"]["conditions"],
                    "products": [{"Name": "test-only", "Version": "test-only"}],
                    "settings": {"StopTime": "12", "Solver": "ode45"},
                }
            )
        )
        if mode() == "bad_metadata":
            Path("metadata.json").write_text("{}")
        paths = ["raw.mat", "metadata.json"]
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

    def quit(self):
        increment(Path("engine-quits.txt"))
        if mode() == "quit_error":
            raise EngineError("Engine cleanup failed")
