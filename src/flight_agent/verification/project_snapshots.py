import hashlib
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ..runtime.project_snapshots import ProjectSnapshot, SnapshotFile, SnapshotManifest
from .intake import safe_directory
from .matlab_contracts import MatlabProjectDescriptor
from .matlab_projects import discover_matlab_project
from .snapshot_lock import lifecycle_lock

_DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_MANIFEST_BYTES = 4_000_000


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read_regular(path: Path, limit: int) -> bytes:
    safe_directory(path.parent)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"Snapshot is not an independent regular file: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Snapshot file changed while opening")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Snapshot manifest exceeds its limit")
    return content


def _file_sha256(path: Path, expected: SnapshotFile) -> None:
    safe_directory(path.parent)
    before = path.lstat()
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
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
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
    if not root.is_dir():
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
        if readonly and directory.stat().st_mode & 0o222:
            raise ValueError("Immutable snapshot directory is writable")
        for name in directories:
            child = directory / name
            if child.is_symlink() or not child.is_dir():
                raise ValueError("Snapshot contains a symlink or special directory")
            actual_dirs.add(child.relative_to(root).as_posix())
        for name in names:
            child = directory / name
            relative = child.relative_to(root).as_posix()
            if relative not in expected_files:
                raise ValueError(f"Unexpected snapshot file: {relative}")
            _file_sha256(child, expected_files[relative])
            if readonly and child.stat().st_mode & 0o222:
                raise ValueError("Immutable snapshot file is writable")
            actual_files.add(relative)
    if actual_files != set(expected_files) or actual_dirs != expected_dirs:
        raise ValueError("Snapshot inventory differs from its manifest")


def _copy_inventory(source: Path, destination: Path, manifest: SnapshotManifest) -> None:
    safe_directory(source)
    safe_directory(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for item in manifest.files:
        original = source / item.path
        target = destination / item.path
        safe_directory(original.parent)
        safe_directory(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = original.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size != item.size:
            raise ValueError(f"Project changed type/size: {item.path}")
        descriptor = os.open(original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
            opened = os.fstat(reader.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
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


def _seal(directory: Path) -> None:
    for current, directories, names in os.walk(directory, topdown=False):
        root = Path(current)
        for name in names:
            (root / name).chmod(0o444)
        for name in directories:
            (root / name).chmod(0o555)
        _sync_directory(root)


def _remove_owned_directory(directory: Path) -> None:
    safe_directory(directory)
    for current, directories, names in os.walk(directory, followlinks=False):
        root = Path(current)
        root.chmod(0o700)
        for name in directories:
            if (root / name).is_symlink():
                raise ValueError("Refusing to remove a symlinked managed directory")
        for name in names:
            child = root / name
            if child.is_symlink() or not child.is_file() or child.lstat().st_nlink != 1:
                raise ValueError("Refusing to remove a special managed file")
            child.chmod(0o600)
    shutil.rmtree(directory)


class ProjectSnapshotStore:
    def __init__(self, root: Path):
        safe_directory(root)
        self.root = root.resolve()

    @contextmanager
    def guard(self, *, exclusive: bool = False) -> Iterator[None]:
        safe_directory(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        with lifecycle_lock(self.root / "lifecycle.lock", exclusive=exclusive):
            for name in ("objects", "staging", "locks"):
                child = self.root / name
                safe_directory(child)
                child.mkdir(exist_ok=True)
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
        if not obj.is_dir() or {item.name for item in obj.iterdir()} != {
            "manifest.json",
            "tree",
        }:
            raise ValueError("Snapshot object is missing or has unexpected members")
        raw = _read_regular(obj / "manifest.json", MAX_MANIFEST_BYTES)
        manifest = SnapshotManifest.model_validate_json(raw)
        if (
            manifest != binding.manifest
            or raw != manifest.canonical_bytes()
            or manifest.snapshot_id != binding.snapshot_id
        ):
            raise ValueError("Snapshot manifest identity mismatch")
        if obj.stat().st_mode & 0o222 or (obj / "manifest.json").stat().st_mode & 0o222:
            raise ValueError("Immutable snapshot object is writable")
        _verify_tree(obj / "tree", manifest, readonly=True)
        return obj / "tree"

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
        if final.exists():
            self.verify(binding)
            if discover_matlab_project(project.root) != project:
                raise ValueError("Project changed during snapshot reuse")
            return binding
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "staging" / f"snap-{uuid4().hex}"
        temporary.mkdir()
        try:
            _copy_inventory(source, temporary / "tree", manifest)
            if discover_matlab_project(project.root) != project:
                raise ValueError("Project changed while copying its snapshot")
            with (temporary / "manifest.json").open("xb") as stream:
                stream.write(manifest.canonical_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            _seal(temporary)
            os.rename(temporary, final)
            final.chmod(0o555)
            _sync_directory(final.parent)
            self.verify(binding)
            return binding
        finally:
            if temporary.exists():
                _remove_owned_directory(temporary)

    def materialize(self, binding: ProjectSnapshot, destination: Path) -> Path:
        safe_directory(destination)
        destination = destination.resolve()
        if destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
            raise ValueError("Mutable work must not overlap immutable snapshots")
        with self.guard():
            immutable = self.verify(binding)
            if destination.exists() or destination.is_symlink():
                raise ValueError("Mutable work destination already exists")
            try:
                _copy_inventory(immutable, destination, binding.manifest)
                self.verify(binding)
            except BaseException:
                if destination.exists():
                    _remove_owned_directory(destination)
                raise
        return destination

    def remove_object(self, binding: ProjectSnapshot) -> None:
        self.verify(binding)
        obj = self.object_directory(binding.snapshot_id)
        _remove_owned_directory(obj)
        _sync_directory(obj.parent)
