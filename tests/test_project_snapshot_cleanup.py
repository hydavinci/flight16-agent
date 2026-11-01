import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest

from flight_agent.runtime.project_snapshots import ProjectSnapshot
from flight_agent.verification.contracts import Conversation
from flight_agent.verification.matlab_contracts import MatlabProjectDescriptor
from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.verification.project_snapshots import ProjectSnapshotStore
from flight_agent.verification.store import ConversationStore
from tests.matlab_project_helpers import prepared_project
from tests.snapshot_helpers import manifest_for, snapshot_case
from tests.test_project_snapshot_references import (
    _record_with_binding as record_with_binding,
)


class ReparseStat:
    def __init__(self, base):
        self._base = base
        self.st_file_attributes = 0x00000400

    def __getattr__(self, name):
        return getattr(self._base, name)


def unpublished_case(tmp_path):
    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    manifest = manifest_for(project)
    binding = ProjectSnapshot(
        snapshot_id=manifest.snapshot_id,
        source_fingerprint=project.fingerprint,
        manifest=manifest,
    )
    return source, project, snapshots, binding


def confirm_from_process(
    snapshot_root,
    store_root,
    project_data,
    record_data,
    published,
    permit_commit,
    results,
):
    try:
        snapshots = ProjectSnapshotStore(Path(snapshot_root))
        store = ConversationStore(Path(store_root))
        project = MatlabProjectDescriptor.model_validate(project_data)
        record = Conversation.model_validate(record_data)
        with snapshots.guard():
            snapshots.ensure(project)
            published.set()
            if not permit_commit.wait(timeout=15):
                raise TimeoutError("confirmation commit was not released")
            store.save(record)
        results.put(("ok", None))
    except BaseException as exc:
        results.put(("error", repr(exc)))


def test_dry_run_and_explicit_orphan_cleanup_preserve_external_directories(tmp_path):
    source, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "report.docx").write_bytes(b"retained external report")
    original = (source / "initialize.m").read_bytes()
    work = store.root / "work"
    work.mkdir()
    (work / "keep.txt").write_text("mutable work is not snapshot garbage")

    report = store.cleanup_project_snapshots()

    assert report.would_delete == [binding.snapshot_id]
    assert report.deleted == []
    snapshots.verify(binding)
    removed = store.cleanup_project_snapshots(dry_run=False)
    assert removed.deleted == [binding.snapshot_id]
    assert not snapshots.object_directory(binding.snapshot_id).exists()
    assert (source / "initialize.m").read_bytes() == original
    assert (delivery / "report.docx").read_bytes() == b"retained external report"
    assert (work / "keep.txt").is_file()


def test_archived_terminal_baseline_retains_snapshot_after_restart(tmp_path):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    record.task.stage = "completed"
    record.archived_tasks.append(record.task)
    record.task = None
    record.status = "completed"
    with snapshots.guard():
        store.save(record)

    restarted = ConversationStore(store.root)
    report = restarted.cleanup_project_snapshots(dry_run=False)

    assert report.retained == [binding.snapshot_id]
    assert report.deleted == []
    snapshots.verify(binding)


def test_corrupt_unreferenced_object_is_reported_not_deleted(tmp_path):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    path = snapshots.object_directory(binding.snapshot_id) / "manifest.json"
    path.chmod(0o644)
    path.write_text("{}")

    report = store.cleanup_project_snapshots(dry_run=False)

    assert binding.snapshot_id in report.skipped
    assert report.deleted == []
    assert path.exists()


def test_unsealed_unreferenced_object_is_reported_not_deleted(tmp_path):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    path = snapshots.object_directory(binding.snapshot_id) / "manifest.json"
    path.chmod(0o644)

    report = store.cleanup_project_snapshots(dry_run=False)

    assert binding.snapshot_id in report.skipped
    assert report.deleted == []
    assert path.exists()


def test_symlinked_object_is_never_followed(tmp_path):
    source, _, snapshots, _ = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    foreign_id = "f" * 64
    shard = snapshots.root / "objects" / "ff"
    shard.mkdir(exist_ok=True)
    (shard / foreign_id).symlink_to(source, target_is_directory=True)

    report = store.cleanup_project_snapshots(dry_run=False)

    assert foreign_id in report.skipped
    assert (source / "initialize.m").is_file()


def test_reparse_object_is_never_followed_or_deleted(tmp_path, monkeypatch):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    obj = snapshots.object_directory(binding.snapshot_id)
    original_lstat = Path.lstat

    def reparse_lstat(path):
        info = original_lstat(path)
        return ReparseStat(info) if path == obj else info

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    report = store.cleanup_project_snapshots(dry_run=False)
    monkeypatch.undo()

    assert binding.snapshot_id in report.skipped
    assert obj.exists()


def test_unknown_managed_entries_are_reported_without_traversal(tmp_path):
    source, _, snapshots, _ = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    unknown_shard = snapshots.root / "objects" / "zz"
    unknown_shard.mkdir()
    (unknown_shard / "external").symlink_to(source, target_is_directory=True)
    special_shard = snapshots.root / "objects" / "00"
    special_shard.write_bytes(b"not a directory")
    invalid_object = snapshots.root / "objects" / "aa" / "not-a-digest"
    invalid_object.parent.mkdir()
    invalid_object.mkdir()

    report = store.cleanup_project_snapshots(dry_run=False)

    assert {"zz", "00", "not-a-digest"} <= set(report.skipped)
    assert (source / "initialize.m").is_file()
    assert unknown_shard.is_dir()
    assert special_shard.is_file()
    assert invalid_object.is_dir()


def test_staging_cleanup_is_reported_in_both_modes_and_uses_owned_directory(tmp_path):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    temporary = snapshots.root / "staging" / f"snap-{'a' * 32}"
    temporary.mkdir()
    (temporary / "partial").write_bytes(b"owned staging data")

    preview = store.cleanup_project_snapshots()

    assert preview.staging == [temporary.name]
    assert temporary.is_dir()
    removed = store.cleanup_project_snapshots(dry_run=False)
    assert removed.staging == [temporary.name]
    assert not temporary.exists()
    assert removed.deleted == [binding.snapshot_id]


def test_staging_with_nested_symlink_is_skipped_without_quarantine(tmp_path):
    source, _, snapshots, _ = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    temporary = snapshots.root / "staging" / f"snap-{'b' * 32}"
    temporary.mkdir()
    (temporary / "external").symlink_to(source, target_is_directory=True)

    report = store.cleanup_project_snapshots(dry_run=False)

    assert temporary.name in report.skipped
    assert temporary.is_dir()
    assert (source / "initialize.m").is_file()
    assert not list(temporary.parent.glob(f".{temporary.name}.quarantine-*"))


def test_missing_reference_index_row_prevents_all_cleanup(tmp_path):
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    with snapshots.guard():
        store.save(record)
    with store.transaction():
        store._write_connection().execute("DELETE FROM project_snapshot_references")

    with pytest.raises(ValueError, match="reference index"):
        store.cleanup_project_snapshots(dry_run=False)

    snapshots.verify(binding)


def test_cleanup_cannot_race_confirmation_before_reference_commit(tmp_path):
    _, project, snapshots, binding = unpublished_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    published = Event()
    permit_commit = Event()
    cleanup_started = Event()

    def confirm():
        with snapshots.guard():
            snapshots.ensure(project)
            published.set()
            assert permit_commit.wait(timeout=10)
            store.save(record)

    def collect():
        cleanup_started.set()
        return store.cleanup_project_snapshots(dry_run=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirming = pool.submit(confirm)
        assert published.wait(timeout=10)
        collecting = pool.submit(collect)
        assert cleanup_started.wait(timeout=10)
        permit_commit.set()
        confirming.result(timeout=10)
        report = collecting.result(timeout=10)

    assert report.deleted == []
    assert report.retained == [binding.snapshot_id]
    snapshots.verify(binding)


def test_process_guard_blocks_cleanup_until_reference_commit(tmp_path, monkeypatch):
    import multiprocessing

    _, project, snapshots, binding = unpublished_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    context = multiprocessing.get_context("spawn")
    published = context.Event()
    permit_commit = context.Event()
    results = context.Queue()
    cleanup_attempted = Event()
    cleanup_acquired = Event()
    original_guard = ProjectSnapshotStore.guard
    original_cleanup = ProjectSnapshotStore.cleanup

    @contextmanager
    def observe_guard(instance, *, exclusive=False):
        if exclusive:
            cleanup_attempted.set()
        with original_guard(instance, exclusive=exclusive):
            yield

    def observe_cleanup(instance, retained_ids, *, dry_run):
        cleanup_acquired.set()
        return original_cleanup(instance, retained_ids, dry_run=dry_run)

    monkeypatch.setattr(ProjectSnapshotStore, "guard", observe_guard)
    monkeypatch.setattr(ProjectSnapshotStore, "cleanup", observe_cleanup)
    process = context.Process(
        target=confirm_from_process,
        args=(
            str(snapshots.root),
            str(store.root),
            project.model_dump(mode="json"),
            record.model_dump(mode="json"),
            published,
            permit_commit,
            results,
        ),
    )
    process.start()
    try:
        assert published.wait(timeout=20)
        with ThreadPoolExecutor(max_workers=1) as pool:
            collecting = pool.submit(store.cleanup_project_snapshots, dry_run=False)
            assert cleanup_attempted.wait(timeout=10)
            assert not cleanup_acquired.wait(timeout=0.2)
            permit_commit.set()
            assert results.get(timeout=20) == ("ok", None)
            process.join(timeout=20)
            assert process.exitcode == 0
            report = collecting.result(timeout=20)
        assert cleanup_acquired.is_set()
        assert report.deleted == []
        assert report.retained == [binding.snapshot_id]
        snapshots.verify(binding)
    finally:
        permit_commit.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link containment check")
def test_linked_external_file_makes_unreferenced_object_ineligible(tmp_path):
    external = tmp_path / "external.txt"
    external.write_bytes(b"external")
    _, _, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    tree = snapshots.object_directory(binding.snapshot_id) / "tree"
    target = tree / "initialize.m"
    tree.chmod(0o755)
    target.chmod(0o600)
    target.unlink()
    os.link(external, target)
    tree.chmod(0o555)

    report = store.cleanup_project_snapshots(dry_run=False)

    assert binding.snapshot_id in report.skipped
    assert report.deleted == []
    assert external.read_bytes() == b"external"
