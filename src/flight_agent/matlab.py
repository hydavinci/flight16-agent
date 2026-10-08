import importlib.util
import math
import os
import signal
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from .models import AnalysisResult, Parameters, validate_result_context


class MatlabError(RuntimeError):
    """MATLAB could not produce a validated result."""


def engine_available() -> bool:
    try:
        return importlib.util.find_spec("matlab.engine") is not None
    except (ImportError, ValueError):
        return False


def _stop_worker(process: subprocess.Popen[str]) -> tuple[str, str]:
    if os.name == "posix" or process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass  # The owned process exited between poll and termination.
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        return process.communicate(timeout=2)
    finally:
        # The worker can exit while descendants survive without inheriting its pipes.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class MatlabRunner:
    def __init__(self, project_root: Path, timeout_seconds: float = 120):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("MATLAB timeout must be finite and greater than zero")
        self.project_root = project_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.log = ""

    def run(self, parameters: Parameters) -> AnalysisResult:
        entry = self.project_root / "matlab" / "analyze_demo.m"
        if not entry.is_file():
            raise MatlabError(f"Trusted MATLAB entry is missing: {entry}")
        process = subprocess.Popen(
            [sys.executable, "-m", "flight_agent.matlab_worker", str(self.project_root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                parameters.model_dump_json(), timeout=self.timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            _, self.log = _stop_worker(process)
            raise MatlabError(
                f"MATLAB timed out after {self.timeout_seconds:g}s; worker termination requested. "
                "Check for detached MATLAB processes before retrying."
            ) from exc
        finally:
            if process.poll() is None:
                _stop_worker(process)
        self.log = stderr
        if process.returncode != 0:
            _stop_worker(process)
            raise MatlabError(f"MATLAB worker failed (exit {process.returncode}): {stderr.strip()}")
        try:
            result = AnalysisResult.model_validate_json(stdout)
            validate_result_context(result, parameters)
            return result
        except (ValidationError, ValueError) as exc:
            raise MatlabError(f"Invalid MATLAB JSON result: {exc}") from exc
