import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .intake import safe_directory


@contextmanager
def lifecycle_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    safe_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Snapshot lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Snapshot lock must be an independent regular file")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            win_dll = getattr(ctypes, "WinDLL")
            get_osfhandle = getattr(msvcrt, "get_osfhandle")
            win_error = getattr(ctypes, "WinError")
            get_last_error = getattr(ctypes, "get_last_error")
            kernel = win_dll("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            handle = wintypes.HANDLE(get_osfhandle(stream.fileno()))
            overlapped = Overlapped()
            flags = 0x00000002 if exclusive else 0
            if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
                raise win_error(get_last_error())
            try:
                yield
            finally:
                if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise win_error(get_last_error())
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(stream.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
