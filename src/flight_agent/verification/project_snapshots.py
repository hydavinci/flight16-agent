import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from ..runtime.project_snapshots import ProjectSnapshot, SnapshotFile, SnapshotManifest
from .intake import safe_directory
from .matlab_contracts import MatlabProjectDescriptor
from .matlab_projects import discover_matlab_project
from .snapshot_lock import is_reparse_point, lifecycle_lock

_DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_MANIFEST_BYTES = 4_000_000
_WINDOWS = os.name == "nt"


def _managed_lstat(path: Path) -> os.stat_result:
    info = path.lstat()
    if is_reparse_point(info):
        raise ValueError(f"Managed snapshot path cannot be a reparse point: {path.name}")
    return info


def _windows_api() -> tuple[Any, Any, Any]:
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL")
    get_last_error = getattr(ctypes, "get_last_error")
    win_error = getattr(ctypes, "WinError")
    kernel = win_dll("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel.FlushFileBuffers.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    kernel.MoveFileExW.restype = wintypes.BOOL
    return kernel, get_last_error, win_error


def _windows_flush_directory(path: Path) -> None:
    kernel, get_last_error, win_error = _windows_api()
    handle = kernel.CreateFileW(
        str(path),
        0x40000000,
        0x00000007,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle in (-1, ctypes.c_void_p(-1).value):
        raise win_error(get_last_error())
    flush_error = None
    if not kernel.FlushFileBuffers(handle):
        flush_error = get_last_error()
    close_error = None
    if not kernel.CloseHandle(handle):
        close_error = get_last_error()
    if flush_error is not None:
        raise win_error(flush_error)
    if close_error is not None:
        raise win_error(close_error)


def _windows_move_no_replace(source: Path, destination: Path) -> None:
    kernel, get_last_error, win_error = _windows_api()
    if not kernel.MoveFileExW(str(source), str(destination), 0x00000008):
        raise win_error(get_last_error())


def _sync_directory(path: Path) -> None:
    info = _managed_lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Snapshot sync requires a real directory")
    if _WINDOWS:
        _windows_flush_directory(path)
    elif os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_regular_file(path: Path) -> None:
    before = _managed_lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Snapshot seal requires an independent regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            is_reparse_point(info)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise ValueError("Snapshot seal requires an independent regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    _directory_identity(source.parent)
    _directory_identity(destination.parent)
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        renamex = library.renamex_np
        renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex.restype = ctypes.c_int
        if renamex(os.fsencode(source), os.fsencode(destination), 0x00000004) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOTSUP, errno.EINVAL):
            raise OSError(error, os.strerror(error), str(destination))
    elif _WINDOWS:
        _windows_move_no_replace(source, destination)
        return
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, "Destination already exists", str(destination))
    os.rename(source, destination)


def _publish_no_replace(source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise ValueError("Snapshot publication must rename within its durable shard")
    _rename_no_replace(source, destination)


def _directory_identity(path: Path) -> tuple[int, int]:
    info = _managed_lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Managed directory must be a real directory")
    return info.st_dev, info.st_ino


def _verify_sealed_mode(path: Path, expected: int) -> None:
    mode = stat.S_IMODE(_managed_lstat(path).st_mode)
    if os.name == "posix":
        if mode != expected:
            raise ValueError(f"Immutable snapshot has unexpected mode: {path.name}")
    elif mode & 0o222:
        raise ValueError(f"Immutable snapshot is writable: {path.name}")


def _read_regular(path: Path, limit: int) -> bytes:
    safe_directory(path.parent)
    _directory_identity(path.parent)
    before = _managed_lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"Snapshot is not an independent regular file: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if is_reparse_point(opened) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValueError("Snapshot file changed while opening")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Snapshot manifest exceeds its limit")
    return content


def _file_sha256(path: Path, expected: SnapshotFile) -> None:
    safe_directory(path.parent)
    _directory_identity(path.parent)
    before = _managed_lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != expected.size
    ):
        raise ValueError(f"Snapshot file type/size mismatch: {expected.path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if is_reparse_point(opened) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValueError("Snapshot file changed while opening")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > expected.size:
                raise ValueError(f"Snapshot grew while reading: {expected.path}")
            digest.update(chunk)
    if size != expected.size or digest.hexdigest() != expected.sha256:
        raise ValueError(f"Snapshot digest mismatch: {expected.path}")


def _verify_tree(root: Path, manifest: SnapshotManifest, *, readonly: bool) -> None:
    safe_directory(root)
    if not stat.S_ISDIR(_managed_lstat(root).st_mode):
        raise ValueError("Snapshot tree is missing")
    expected_files = {item.path: item for item in manifest.files}
    expected_dirs = {
        parent.as_posix()
        for name in expected_files
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        directory = Path(current)
        if not stat.S_ISDIR(_managed_lstat(directory).st_mode):
            raise ValueError("Snapshot contains a special directory")
        if readonly:
            _verify_sealed_mode(directory, 0o555)
        for name in directories:
            child = directory / name
            if not stat.S_ISDIR(_managed_lstat(child).st_mode):
                raise ValueError("Snapshot contains a symlink or special directory")
            if readonly:
                _verify_sealed_mode(child, 0o555)
            actual_dirs.add(child.relative_to(root).as_posix())
        for name in names:
            child = directory / name
            relative = child.relative_to(root).as_posix()
            if relative not in expected_files:
                raise ValueError(f"Unexpected snapshot file: {relative}")
            _file_sha256(child, expected_files[relative])
            if readonly:
                _verify_sealed_mode(child, 0o444)
            actual_files.add(relative)
    if actual_files != set(expected_files) or actual_dirs != expected_dirs:
        raise ValueError("Snapshot inventory differs from its manifest")


def _copy_inventory(
    source: Path,
    destination: Path,
    manifest: SnapshotManifest,
    *,
    create: bool = True,
) -> None:
    safe_directory(source)
    safe_directory(destination)
    if create:
        destination.mkdir(parents=True, exist_ok=False)
    elif not stat.S_ISDIR(_managed_lstat(destination).st_mode):
        raise ValueError("Mutable work destination changed type")
    _directory_identity(destination)
    for item in manifest.files:
        original = source / item.path
        target = destination / item.path
        safe_directory(original.parent)
        safe_directory(target.parent)
        _directory_identity(original.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        _directory_identity(target.parent)
        before = _managed_lstat(original)
        if not stat.S_ISREG(before.st_mode) or before.st_size != item.size:
            raise ValueError(f"Project changed type/size: {item.path}")
        descriptor = os.open(original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
            opened = os.fstat(reader.fileno())
            if is_reparse_point(opened) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise ValueError("Project changed while opening")
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                size += len(chunk)
                if size > item.size:
                    raise ValueError(f"Project grew while copying: {item.path}")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if size != item.size or digest.hexdigest() != item.sha256:
            raise ValueError(f"Project changed while copying: {item.path}")
        target.chmod(0o644)
    _verify_tree(destination, manifest, readonly=False)


def _seal_inventory(directory: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    directories: list[Path] = []

    def collect(root: Path) -> None:
        if not stat.S_ISDIR(_managed_lstat(root).st_mode):
            raise ValueError("Snapshot seal encountered a special directory")
        with os.scandir(root) as entries:
            for entry in entries:
                child = root / entry.name
                info = _managed_lstat(child)
                if stat.S_ISDIR(info.st_mode):
                    collect(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files.append(child)
                else:
                    raise ValueError(
                        "Snapshot seal encountered a special or linked file"
                    )
        directories.append(root)

    collect(directory)
    return files, directories


def _seal(directory: Path) -> None:
    files, directories = _seal_inventory(directory)
    for child in files:
        info = _managed_lstat(child)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Snapshot seal encountered a special or linked file")
        child.chmod(0o444)
        _sync_regular_file(child)
    for root in directories:
        if not stat.S_ISDIR(_managed_lstat(root).st_mode):
            raise ValueError("Snapshot seal encountered a special directory")
        root.chmod(0o555)
        _sync_directory(root)


def _remove_quarantined_directory(
    directory: Path, expected_identity: tuple[int, int]
) -> None:
    if _directory_identity(directory) != expected_identity:
        raise ValueError("Quarantined directory identity changed")
    for current, directories, names in os.walk(directory, followlinks=False):
        root = Path(current)
        if not stat.S_ISDIR(_managed_lstat(root).st_mode):
            raise ValueError("Refusing to remove a special managed directory")
        for name in directories:
            child = root / name
            if not stat.S_ISDIR(_managed_lstat(child).st_mode):
                raise ValueError("Refusing to remove a symlinked managed directory")
        for name in names:
            child = root / name
            info = _managed_lstat(child)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Refusing to remove a special managed file")
            child.chmod(0o600)
        root.chmod(0o700)
    shutil.rmtree(directory)


def _quarantine_and_remove(
    directory: Path, *, expected_identity: tuple[int, int] | None = None
) -> None:
    safe_directory(directory.parent)
    identity = _directory_identity(directory)
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("Managed directory identity changed before cleanup")
    while True:
        quarantine = directory.with_name(f".{directory.name}.quarantine-{uuid4().hex}")
        try:
            _rename_no_replace(directory, quarantine)
        except FileExistsError:
            continue
        break
    if _directory_identity(quarantine) != identity:
        raise ValueError("Managed directory identity changed while quarantining")
    _sync_directory(directory.parent)
    _remove_quarantined_directory(quarantine, identity)
    _sync_directory(directory.parent)


def _cleanup_if_owned(directory: Path, identity: tuple[int, int]) -> None:
    try:
        current = _directory_identity(directory)
    except (FileNotFoundError, ValueError):
        return
    if current == identity:
        _quarantine_and_remove(directory, expected_identity=identity)


def _verify_object(
    obj: Path, binding: ProjectSnapshot, *, validate_binding: bool = True
) -> Path:
    if validate_binding:
        binding = ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
    safe_directory(obj)
    if not stat.S_ISDIR(_managed_lstat(obj).st_mode) or {
        item.name for item in obj.iterdir()
    } != {"manifest.json", "tree"}:
        raise ValueError("Snapshot object is missing or has unexpected members")
    raw = _read_regular(obj / "manifest.json", MAX_MANIFEST_BYTES)
    manifest = SnapshotManifest.model_validate_json(raw)
    if (
        manifest != binding.manifest
        or raw != manifest.canonical_bytes()
        or manifest.snapshot_id != binding.snapshot_id
    ):
        raise ValueError("Snapshot manifest identity mismatch")
    _verify_sealed_mode(obj, 0o555)
    _verify_sealed_mode(obj / "manifest.json", 0o444)
    _verify_tree(obj / "tree", manifest, readonly=True)
    return obj / "tree"


class ProjectSnapshotStore:
    def __init__(self, root: Path):
        safe_directory(root)
        self.root = root.resolve()

    @contextmanager
    def guard(self, *, exclusive: bool = False) -> Iterator[None]:
        safe_directory(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _directory_identity(self.root)
        with lifecycle_lock(self.root / "lifecycle.lock", exclusive=exclusive):
            created = False
            for name in ("objects", "staging", "locks"):
                child = self.root / name
                safe_directory(child)
                try:
                    child.mkdir()
                except FileExistsError:
                    pass
                else:
                    created = True
                _directory_identity(child)
            if created:
                _sync_directory(self.root)
            yield

    def object_directory(self, snapshot_id: str) -> Path:
        if _DIGEST.fullmatch(snapshot_id) is None:
            raise ValueError("Invalid snapshot ID")
        directory = self.root / "objects" / snapshot_id[:2] / snapshot_id
        safe_directory(directory)
        return directory

    def verify(self, binding: ProjectSnapshot) -> Path:
        binding = ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
        obj = self.object_directory(binding.snapshot_id)
        return _verify_object(obj, binding, validate_binding=False)

    def ensure(self, project: MatlabProjectDescriptor) -> ProjectSnapshot:
        project = MatlabProjectDescriptor.model_validate(project.model_dump(mode="json"))
        manifest = SnapshotManifest(
            files=[
                SnapshotFile.model_validate(item.model_dump(mode="json"))
                for item in project.files
            ]
        )
        lock_path = self.root / "locks" / f"{manifest.snapshot_id}.lock"
        with lifecycle_lock(lock_path, exclusive=True):
            return self._ensure_locked(project)

    def _ensure_locked(self, project: MatlabProjectDescriptor) -> ProjectSnapshot:
        project = MatlabProjectDescriptor.model_validate(project.model_dump(mode="json"))
        source = Path(project.root)
        safe_directory(source)
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise ValueError("Managed snapshots must not overlap the source project")
        if discover_matlab_project(project.root) != project:
            raise ValueError("Project changed; confirm a new resource fingerprint")
        manifest = SnapshotManifest(
            files=[
                SnapshotFile.model_validate(item.model_dump(mode="json"))
                for item in project.files
            ]
        )
        binding = ProjectSnapshot(
            snapshot_id=manifest.snapshot_id,
            source_fingerprint=project.fingerprint,
            manifest=manifest,
        )
        final = self.object_directory(binding.snapshot_id)
        if final.exists() or final.is_symlink():
            self.verify(binding)
            if discover_matlab_project(project.root) != project:
                raise ValueError("Project changed during snapshot reuse")
            return binding
        objects = self.root / "objects"
        shard = final.parent
        safe_directory(shard)
        shard.mkdir(exist_ok=True)
        _directory_identity(shard)
        _sync_directory(objects)
        temporary = self.root / "staging" / f"snap-{uuid4().hex}"
        temporary.mkdir()
        temporary_identity = _directory_identity(temporary)
        try:
            _copy_inventory(source, temporary / "tree", manifest)
            if discover_matlab_project(project.root) != project:
                raise ValueError("Project changed while copying its snapshot")
            with (temporary / "manifest.json").open("xb") as stream:
                stream.write(manifest.canonical_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            shard_temporary = shard / f".publish-{uuid4().hex}"
            _rename_no_replace(temporary, shard_temporary)
            temporary = shard_temporary
            _sync_directory(self.root / "staging")
            _sync_directory(shard)
            _seal(temporary)
            _verify_object(temporary, binding)
            _publish_no_replace(temporary, final)
            _sync_directory(shard)
            self.verify(binding)
            return binding
        finally:
            _cleanup_if_owned(temporary, temporary_identity)

    def materialize(self, binding: ProjectSnapshot, destination: Path) -> Path:
        safe_directory(destination)
        destination = destination.resolve()
        if destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
            raise ValueError("Mutable work must not overlap immutable snapshots")
        with self.guard():
            immutable = self.verify(binding)
            if destination.exists() or destination.is_symlink():
                raise ValueError("Mutable work destination already exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            safe_directory(destination.parent)
            try:
                destination.mkdir()
            except FileExistsError as exc:
                raise ValueError("Mutable work destination already exists") from exc
            identity = _directory_identity(destination)
            try:
                _copy_inventory(immutable, destination, binding.manifest, create=False)
                self.verify(binding)
            except BaseException:
                _cleanup_if_owned(destination, identity)
                raise
        return destination

    def remove_object(self, binding: ProjectSnapshot) -> None:
        self.verify(binding)
        obj = self.object_directory(binding.snapshot_id)
        identity = _directory_identity(obj)
        _quarantine_and_remove(obj, expected_identity=identity)
