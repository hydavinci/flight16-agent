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


class _WindowsProcess(NamedTuple):
    parent_pid: int
    creation_time: str


def engine_available() -> bool:
    try:
        return importlib.util.find_spec("matlab.engine") is not None
    except (ImportError, ValueError):
        return False


def _windows_processes() -> dict[int, _WindowsProcess]:
    command = (
        "$ErrorActionPreference='Stop';"
        "$items=@(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {"
        "[pscustomobject]@{ProcessId=[int]$_.ProcessId;"
        "ParentProcessId=[int]$_.ParentProcessId;"
        "CreationTime=$_.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()}});"
        "ConvertTo-Json -InputObject $items -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise OSError(result.stderr.strip() or "PowerShell process enumeration failed")
    if not result.stdout.strip():
        raise OSError("PowerShell process enumeration returned empty output")
    decoded = json.loads(result.stdout)
    records = decoded if isinstance(decoded, list) else [decoded]
    if not records:
        raise OSError("PowerShell process enumeration returned no processes")
    processes: dict[int, _WindowsProcess] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid Windows process enumeration record")
        creation_time = str(record.get("CreationTime", ""))
        if not creation_time or not creation_time.isdecimal():
            raise ValueError("Windows process identity is unavailable")
        processes[int(record["ProcessId"])] = _WindowsProcess(
            parent_pid=int(record["ParentProcessId"]),
            creation_time=creation_time,
        )
    return processes


def _windows_process_creation_time(process: subprocess.Popen[str]) -> str:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("low", wintypes.DWORD),
            ("high", wintypes.DWORD),
        ]

    handle = getattr(process, "_handle", None)
    if handle is None:
        raise OSError("Windows worker process handle is unavailable")
    win_dll = getattr(ctypes, "WinDLL")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel = win_dll("kernel32", use_last_error=True)
    kernel.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    created = FileTime()
    exited = FileTime()
    kernel_time = FileTime()
    user_time = FileTime()
    if not kernel.GetProcessTimes(
        int(handle),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise OSError(
            f"GetProcessTimes({process.pid}) failed with Windows error {get_last_error()}"
        )
    return str((created.high << 32) | created.low)


def _windows_terminate_pid(pid: int, creation_time: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("low", wintypes.DWORD),
            ("high", wintypes.DWORD),
        ]

    win_dll = getattr(ctypes, "WinDLL")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel = win_dll("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    access = 0x0001 | 0x1000 | 0x00100000
    handle = kernel.OpenProcess(access, False, pid)
    if not handle:
        error = get_last_error()
        if error == 87:
            return None
        return f"OpenProcess({pid}) failed with Windows error {error}"
    try:
        created = FileTime()
        exited = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return f"GetProcessTimes({pid}) failed with Windows error {get_last_error()}"
        actual_creation = str((created.high << 32) | created.low)
        if actual_creation != creation_time:
            return f"process identity changed for PID {pid}"
        if not kernel.TerminateProcess(handle, 1):
            return f"TerminateProcess({pid}) failed with Windows error {get_last_error()}"
        wait_result = kernel.WaitForSingleObject(handle, 2000)
        if wait_result != 0:
            return f"process {pid} did not terminate within the Windows cleanup deadline"
        return None
    finally:
        kernel.CloseHandle(handle)


def _update_windows_tree(
    processes: dict[int, _WindowsProcess],
    depths: dict[int, int],
    identities: dict[int, str],
    reused_at: dict[int, int],
) -> tuple[dict[int, int], str | None]:
    for pid, process in processes.items():
        if not process.creation_time or not process.creation_time.isdecimal():
            return {}, f"Windows process identity is unavailable for PID {pid}"
        if pid not in identities:
            continue
        expected = identities[pid]
        if expected != process.creation_time:
            replacement_time = int(process.creation_time)
            reused_at[pid] = min(reused_at.get(pid, replacement_time), replacement_time)
    changed = True
    while changed:
        changed = False
        for pid, process in processes.items():
            parent_pid = process.parent_pid
            if parent_pid not in depths or pid in depths:
                continue
            child_time = int(process.creation_time)
            parent_time = int(identities[parent_pid])
            if child_time == parent_time:
                return {}, (
                    f"Windows process ancestry is uncertain for PID {pid}; "
                    f"it has the same creation time as parent PID {parent_pid}"
                )
            if child_time < parent_time:
                continue
            if parent_pid in reused_at:
                replacement_time = reused_at[parent_pid]
                if child_time == replacement_time:
                    return {}, (
                        f"Windows process ancestry is uncertain for PID {pid} "
                        f"after parent PID {parent_pid} reuse"
                    )
                if child_time > replacement_time:
                    continue
            elif parent_pid not in processes:
                return {}, (
                    f"Windows process ancestry is uncertain for PID {pid}; "
                    f"parent PID {parent_pid} is unavailable and the process "
                    "is still running"
                )
            depths[pid] = depths[parent_pid] + 1
            identities[pid] = process.creation_time
            changed = True
    return (
        {
            pid: depth
            for pid, depth in depths.items()
            if pid in processes
            and pid not in reused_at
            and identities[pid] == processes[pid].creation_time
        },
        None,
    )


def _stop_windows_tree(root_pid: int, root_creation_time: str) -> str | None:
    if not root_creation_time.isdecimal():
        return "Windows root process identity is unavailable"
    depths = {root_pid: 0}
    identities = {root_pid: root_creation_time}
    reused_at: dict[int, int] = {}
    termination_errors: dict[int, str] = {}
    for _ in range(3):
        try:
            processes = _windows_processes()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return f"cannot enumerate the Windows process tree: {exc}"
        live, identity_error = _update_windows_tree(
            processes, depths, identities, reused_at
        )
        if identity_error is not None:
            return identity_error
        if not live:
            return None
        for pid in sorted(live, key=live.__getitem__, reverse=True):
            try:
                error = _windows_terminate_pid(pid, identities[pid])
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)
            if error is not None:
                termination_errors[pid] = error
        time.sleep(0.05)
    try:
        processes = _windows_processes()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"cannot verify the Windows process tree termination: {exc}"
    live, identity_error = _update_windows_tree(
        processes, depths, identities, reused_at
    )
    if identity_error is not None:
        return identity_error
    remaining = sorted(live)
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

    windows_creation_time: str | None = None
    if os.name == "nt":
        try:
            windows_creation_time = _windows_process_creation_time(process)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"cannot bind the Windows worker process identity: {exc}")
        if windows_creation_time is not None:
            if error := _stop_windows_tree(process.pid, windows_creation_time):
                errors.append(error)
    else:
        send_signal(signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            if windows_creation_time is not None:
                if error := _stop_windows_tree(process.pid, windows_creation_time):
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
