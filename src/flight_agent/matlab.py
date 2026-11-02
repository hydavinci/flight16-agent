import importlib.util
import json
import logging
import os
import signal
import subprocess
import time
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


def _windows_processes() -> dict[int, int]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "PowerShell process enumeration failed")
    if not result.stdout.strip():
        return {}
    decoded = json.loads(result.stdout)
    records = decoded if isinstance(decoded, list) else [decoded]
    return {
        int(record["ProcessId"]): int(record["ParentProcessId"])
        for record in records
        if isinstance(record, dict)
        and "ProcessId" in record
        and "ParentProcessId" in record
    }


def _windows_terminate_pid(pid: int) -> str | None:
    result = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or result.stdout.strip() or f"taskkill exited {result.returncode}"


def _expand_windows_tree(
    processes: dict[int, int], depths: dict[int, int]
) -> None:
    changed = True
    while changed:
        changed = False
        for pid, parent_pid in processes.items():
            if parent_pid in depths and pid not in depths:
                depths[pid] = depths[parent_pid] + 1
                changed = True


def _stop_windows_tree(root_pid: int) -> str | None:
    depths = {root_pid: 0}
    termination_errors: dict[int, str] = {}
    for _ in range(3):
        try:
            processes = _windows_processes()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return f"cannot enumerate the Windows process tree: {exc}"
        _expand_windows_tree(processes, depths)
        live = {pid: depth for pid, depth in depths.items() if pid in processes}
        if not live:
            return None
        for pid in sorted(live, key=live.__getitem__, reverse=True):
            try:
                error = _windows_terminate_pid(pid)
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)
            if error is not None:
                termination_errors[pid] = error
        time.sleep(0.05)
    try:
        processes = _windows_processes()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"cannot verify the Windows process tree termination: {exc}"
    _expand_windows_tree(processes, depths)
    remaining = sorted(pid for pid in depths if pid in processes)
    if not remaining:
        return None
    details = "; ".join(
        f"{pid}: {termination_errors.get(pid, 'termination not confirmed')}"
        for pid in remaining
    )
    return f"Windows process tree still running after termination: {details}"


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

    if os.name == "nt":
        if error := _stop_windows_tree(process.pid):
            errors.append(error)
    else:
        send_signal(signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            if error := _stop_windows_tree(process.pid):
                errors.append(error)
        else:
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
