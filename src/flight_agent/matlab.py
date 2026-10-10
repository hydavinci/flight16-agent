import importlib.util
import logging
import math
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from pydantic import ValidationError

from .models import AnalysisResult, Parameters, validate_result_context

logger = logging.getLogger(__name__)


class MatlabError(RuntimeError):
    """MATLAB could not produce a validated result."""


class _CleanupResult(NamedTuple):
    stdout: str
    stderr: str
    error: str | None


def engine_available() -> bool:
    try:
        return importlib.util.find_spec("matlab.engine") is not None
    except (ImportError, ValueError):
        return False


def _stop_worker(process: subprocess.Popen[str]) -> _CleanupResult:
    errors: list[str] = []
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None

    def send_signal(sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif process.poll() is not None:
                return
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass  # The owned process exited between poll and termination.
        except PermissionError as exc:
            detail = f"{sig.name}: {exc}"
            if detail not in errors:
                errors.append(detail)

    send_signal(signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        send_signal(signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout, exc.stderr
            errors.append("Worker did not exit within the cleanup deadline")
    finally:
        # The worker can exit while descendants survive without inheriting its pipes.
        if os.name == "posix":
            send_signal(signal.SIGKILL)
    error = None
    if errors:
        error = (
            f"MATLAB cleanup incomplete for process group {process.pid}: {'; '.join(errors)}. "
            "Check the worker and its descendants before retrying."
        )
        logger.error(error)
    return _CleanupResult(
        stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout or "",
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr or "",
        error,
    )


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
        cleanup: _CleanupResult | None = None
        try:
            stdout, stderr = process.communicate(
                parameters.model_dump_json(), timeout=self.timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            cleanup = _stop_worker(process)
            self.log = cleanup.stderr
            message = (
                f"MATLAB timed out after {self.timeout_seconds:g}s; worker termination requested. "
                "Check for detached MATLAB processes before retrying."
            )
            if cleanup.error:
                self.log += "\n" + cleanup.error
                message += "\n" + cleanup.error
            raise MatlabError(message) from exc
        finally:
            if process.poll() is None and cleanup is None:
                cleanup = _stop_worker(process)
                self.log = cleanup.stderr
                if cleanup.error:
                    self.log += "\n" + cleanup.error
        self.log = stderr
        if process.returncode != 0:
            cleanup = _stop_worker(process)
            if cleanup.error:
                self.log += "\n" + cleanup.error
            raise MatlabError(
                f"MATLAB worker failed (exit {process.returncode}): {self.log.strip()}"
            )
        try:
            result = AnalysisResult.model_validate_json(stdout)
            validate_result_context(result, parameters)
            return result
        except (ValidationError, ValueError) as exc:
            raise MatlabError(f"Invalid MATLAB JSON result: {exc}") from exc
