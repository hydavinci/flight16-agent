import os
import subprocess
import sys
from heapq import nsmallest
from pathlib import Path
from stat import S_ISDIR

from pydantic import BaseModel, ConfigDict, Field

MAX_DIRECTORIES = 500

_NATIVE_DIRECTORY_SCRIPT = """
on run argv
    try
        activate
        set initialLocation to POSIX file (item 1 of argv)
        set promptText to item 2 of argv
        set selectedFolder to choose folder with prompt promptText default location initialLocation
        return POSIX path of selectedFolder
    on error number -128
        return ""
    end try
end run
"""


class NativeDirectoryError(RuntimeError):
    pass


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=1024)
    path: str = Field(min_length=1, max_length=4096)


class DirectoryListing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=4096)
    parent: str | None = Field(min_length=1, max_length=4096)
    directories: list[DirectoryEntry] = Field(max_length=MAX_DIRECTORIES)
    truncated: bool


def _resolve_directory(path: str) -> Path:
    root = Path(path) if path else Path.home()
    if not root.is_absolute():
        raise ValueError("Directory path must be absolute")
    try:
        root = root.resolve(strict=True)
    except RuntimeError as exc:
        # Python 3.11/3.12 report symlink loops as RuntimeError.
        raise ValueError(str(exc)) from exc
    if not S_ISDIR(root.stat().st_mode):
        raise NotADirectoryError(f"Not a directory: {root}")
    return root


def choose_native_directory(path: str = "", *, title: str = "Select directory") -> str | None:
    if sys.platform != "darwin":
        raise NativeDirectoryError("Native directory selection currently requires macOS")
    root = _resolve_directory(path)
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", _NATIVE_DIRECTORY_SCRIPT, str(root), title],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise NativeDirectoryError(
            "Native directory selection timed out after 120 seconds"
        ) from exc
    except OSError as exc:
        raise NativeDirectoryError(f"Could not open the native directory picker: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise NativeDirectoryError(
            f"Native directory selection failed: {detail or result.returncode}"
        )
    selected = result.stdout.decode("utf-8").removesuffix("\n")
    return str(_resolve_directory(selected)) if selected else None


def browse_directories(path: str = "") -> DirectoryListing:
    root = _resolve_directory(path)
    with os.scandir(root) as entries:
        directories = nsmallest(
            MAX_DIRECTORIES + 1,
            (
                DirectoryEntry(name=entry.name, path=str(root / entry.name))
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ),
            key=lambda entry: (entry.name.casefold(), entry.name),
        )
    return DirectoryListing(
        path=str(root),
        parent=str(root.parent) if root.parent != root else None,
        directories=directories[:MAX_DIRECTORIES],
        truncated=len(directories) > MAX_DIRECTORIES,
    )
