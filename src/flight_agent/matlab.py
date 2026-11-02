import importlib.util
import logging
import os
import signal
import subprocess
from typing import NamedTuple

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


def _stop_worker(process: subprocess.Popen[str], *, label: str = "MATLAB") -> _CleanupResult:
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
            f"{label} cleanup incomplete for process group {process.pid}: {'; '.join(errors)}. "
            "Check the worker and its descendants before retrying."
        )
        logger.error(error)
    return _CleanupResult(
        stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout or "",
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr or "",
        error,
    )
