import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from flight_agent.runtime.project_snapshots import ProjectSnapshot
from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.verification.project_snapshots import ProjectSnapshotStore
from tests.matlab_project_helpers import prepared_project
from tests.snapshot_helpers import manifest_for, snapshot_case


def unpublished_case(tmp_path):
    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    manifest = manifest_for(project)
    binding = ProjectSnapshot(
        snapshot_id=manifest.snapshot_id,
        source_fingerprint=project.fingerprint,
        manifest=manifest,
    )
    return source, project, store, binding


def assert_sealed_object(store, binding):
    tree = store.verify(binding)
    if os.name == "posix":
        obj = store.object_directory(binding.snapshot_id)
        assert obj.stat().st_mode & 0o777 == 0o555
        assert (obj / "manifest.json").stat().st_mode & 0o777 == 0o444
        for current, directories, names in os.walk(tree):
            root = Path(current)
            assert root.stat().st_mode & 0o777 == 0o555
            for name in directories:
                assert (root / name).stat().st_mode & 0o777 == 0o555
            for name in names:
                assert (root / name).stat().st_mode & 0o777 == 0o444


def test_verified_reuse_does_not_replace_object(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    inode = store.object_directory(binding.snapshot_id).stat().st_ino
    with store.guard():
        reused = store.ensure(project)
    assert reused == binding
    assert store.object_directory(binding.snapshot_id).stat().st_ino == inode
    assert source.is_dir()
    assert list((store.root / "staging").iterdir()) == []


def test_same_content_at_another_root_has_same_object_identity(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    second = tmp_path / "another-source"
    shutil.copytree(source, second)
    with store.guard():
        reused = store.ensure(discover_matlab_project(str(second)))
    assert reused.snapshot_id == binding.snapshot_id


def test_source_changed_before_confirmation_leaves_no_object(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    empty = ProjectSnapshotStore(tmp_path / "empty" / "snapshots")
    (source / "initialize.m").write_text("different = 1;\n")
    with empty.guard(), pytest.raises(ValueError, match="changed"):
        empty.ensure(project)
    assert not list((empty.root / "objects").glob("*/*"))
    assert not list((empty.root / "staging").iterdir())
    store.verify(binding)


def test_destination_creation_race_preserves_competing_directory(tmp_path, monkeypatch):
    source, project, store, binding = snapshot_case(tmp_path)
    work = tmp_path / "work" / "project"
    marker = work / "keep.txt"
    original_mkdir = Path.mkdir
    raced = False

    def competing_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        nonlocal raced
        if path == work and not raced:
            raced = True
            original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)
            marker.write_bytes(b"competing owner")
            raise FileExistsError("injected destination creation race")
        return original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", competing_mkdir)
    with pytest.raises((FileExistsError, ValueError)):
        store.materialize(binding, work)
    assert marker.read_bytes() == b"competing owner"
    store.verify(binding)


def test_managed_removal_quarantines_before_recursive_delete(tmp_path, monkeypatch):
    source, project, store, binding = snapshot_case(tmp_path)
    obj = store.object_directory(binding.snapshot_id)
    removed = []
    original_rmtree = shutil.rmtree

    def inspect_quarantine(path, *args, **kwargs):
        path = Path(path)
        assert path != obj
        assert path.parent == obj.parent
        removed.append(path)
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", inspect_quarantine)
    with store.guard(exclusive=True):
        store.remove_object(binding)
    assert len(removed) == 1
    assert not obj.exists()
    assert source.is_dir()


@pytest.mark.parametrize("damage", ["top-level", "member"])
def test_removal_substitution_never_traverses_external_target(tmp_path, monkeypatch, damage):
    from flight_agent.verification import project_snapshots as module

    source, project, store, binding = snapshot_case(tmp_path)
    obj = store.object_directory(binding.snapshot_id)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_bytes(b"external")
    original_quarantine = module._quarantine_and_remove

    def substitute_before_quarantine(directory, *args, **kwargs):
        directory = Path(directory)
        if directory == obj:
            if damage == "top-level":
                for current, directories, names in os.walk(obj, topdown=False):
                    root = Path(current)
                    for name in names:
                        (root / name).chmod(0o600)
                    for name in directories:
                        (root / name).chmod(0o700)
                    root.chmod(0o700)
                shutil.rmtree(obj)
                obj.symlink_to(external, target_is_directory=True)
            else:
                tree = obj / "tree"
                tree.chmod(0o755)
                target = tree / "initialize.m"
                target.chmod(0o600)
                target.unlink()
                target.symlink_to(marker)
                tree.chmod(0o555)
        return original_quarantine(directory, *args, **kwargs)

    monkeypatch.setattr(module, "_quarantine_and_remove", substitute_before_quarantine)
    with store.guard(exclusive=True), pytest.raises(ValueError):
        store.remove_object(binding)
    assert marker.read_bytes() == b"external"
    assert source.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
@pytest.mark.parametrize(
    ("relative", "mode"),
    [("manifest.json", 0o400), ("tree", 0o500), ("tree/initialize.m", 0o400)],
)
def test_verification_requires_exact_sealed_modes(tmp_path, relative, mode):
    source, project, store, binding = snapshot_case(tmp_path)
    target = store.object_directory(binding.snapshot_id) / relative
    target.chmod(mode)
    with pytest.raises(ValueError, match="mode"):
        store.verify(binding)
    assert source.is_dir()


@pytest.mark.parametrize(
    "damage",
    [
        "bytes",
        "extra",
        "ignored",
        "symlink",
        "manifest",
        "missing",
        "renamed",
        "hardlink",
        "directory",
    ],
)
def test_managed_tamper_is_never_reused(tmp_path, damage):
    source, project, store, binding = snapshot_case(tmp_path)
    obj = store.object_directory(binding.snapshot_id)
    tree = obj / "tree"
    tree.chmod(0o755)
    if damage == "manifest":
        path = obj / "manifest.json"
        path.chmod(0o644)
        path.write_text("{}")
    elif damage in {"extra", "ignored"}:
        (tree / (".DS_Store" if damage == "ignored" else "unexpected.m")).write_text("x")
    elif damage == "directory":
        (tree / "unexpected-empty-directory").mkdir()
    else:
        path = tree / "initialize.m"
        path.chmod(0o644)
        if damage == "renamed":
            path.rename(tree / "renamed.m")
        else:
            path.unlink()
            if damage == "symlink":
                path.symlink_to(source / "initialize.m")
            elif damage == "hardlink":
                os.link(source / "initialize.m", path)
            elif damage == "bytes":
                path.write_text("changed")
                path.chmod(0o444)
    tree.chmod(0o555)
    with store.guard(), pytest.raises(ValueError):
        store.ensure(project)
    assert obj.exists()  # No automatic repair or replacement of a corrupt identity.


def test_work_copy_is_independent_and_source_is_not_reopened(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    shutil.rmtree(source)
    work = tmp_path / "work" / "project"
    store.materialize(binding, work)
    immutable = store.verify(binding)
    assert (work / "initialize.m").stat().st_ino != (
        immutable / "initialize.m"
    ).stat().st_ino
    (work / "initialize.m").write_text("mutable = 1;\n")
    assert (immutable / "initialize.m").read_text() != "mutable = 1;\n"
    assert os.access(work / "initialize.m", os.W_OK)
    store.verify(binding)


def test_existing_work_directory_is_never_removed_on_copy_refusal(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    work = tmp_path / "work" / "project"
    work.mkdir(parents=True)
    marker = work / "keep.txt"
    marker.write_bytes(b"preexisting work")
    with pytest.raises(ValueError, match="already exists"):
        store.materialize(binding, work)
    assert marker.read_bytes() == b"preexisting work"
    store.verify(binding)


def test_two_store_instances_publish_one_complete_object(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    with store.guard(exclusive=True):
        store.remove_object(binding)
    other = ProjectSnapshotStore(store.root)
    barrier = Barrier(2)

    def create(instance):
        barrier.wait()
        with instance.guard():
            return instance.ensure(project)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(create, store)
        right = pool.submit(create, other)
        assert left.result(timeout=10) == right.result(timeout=10) == binding
    assert len(list((store.root / "objects").glob("*/*"))) == 1


def test_complete_object_is_sealed_and_shard_is_durable_before_publication(
    tmp_path, monkeypatch
):
    from flight_agent.verification import project_snapshots as module

    source, project, store, binding = unpublished_case(tmp_path)
    synchronized = []
    published = []
    original_sync = module._sync_directory
    original_publish = module._publish_no_replace

    def record_sync(path):
        synchronized.append(Path(path))
        original_sync(path)

    def inspect_publish(source, destination):
        source = Path(source)
        assert store.root / "objects" in synchronized
        if os.name == "posix":
            assert source.stat().st_mode & 0o777 == 0o555
            assert (source / "manifest.json").stat().st_mode & 0o777 == 0o444
            assert (source / "tree").stat().st_mode & 0o777 == 0o555
        original_publish(source, destination)
        published.append(Path(destination))

    monkeypatch.setattr(module, "_sync_directory", record_sync)
    monkeypatch.setattr(module, "_publish_no_replace", inspect_publish)
    with store.guard():
        assert store.ensure(project) == binding
    assert published == [store.object_directory(binding.snapshot_id)]
    assert_sealed_object(store, binding)
    assert source.is_dir()


@pytest.mark.parametrize("failure", ["sync", "verify"])
def test_post_publication_failure_leaves_only_a_complete_sealed_object(
    tmp_path, monkeypatch, failure
):
    from flight_agent.verification import project_snapshots as module

    source, project, store, binding = unpublished_case(tmp_path)
    final = store.object_directory(binding.snapshot_id)
    published = False
    original_publish = module._publish_no_replace
    original_sync = module._sync_directory
    original_verify = store.verify

    def record_publish(source, destination):
        nonlocal published
        original_publish(source, destination)
        published = True

    def fail_after_publish(path):
        if published:
            raise OSError("injected post-publication sync failure")
        original_sync(path)

    def fail_verification(value):
        raise OSError("injected post-publication verification failure")

    monkeypatch.setattr(module, "_publish_no_replace", record_publish)
    if failure == "sync":
        monkeypatch.setattr(module, "_sync_directory", fail_after_publish)
        message = "sync"
    else:
        monkeypatch.setattr(store, "verify", fail_verification)
        message = "verification"
    with store.guard(), pytest.raises(OSError, match=message):
        store.ensure(project)
    assert final.is_dir()
    assert original_verify(binding) == final / "tree"
    monkeypatch.setattr(store, "verify", original_verify)
    assert_sealed_object(store, binding)
    assert list((store.root / "staging").iterdir()) == []
    assert {item.name for item in final.parent.iterdir()} == {final.name}
    assert source.is_dir()


def test_destination_appearance_before_publication_is_not_replaced(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module

    source, project, store, binding = unpublished_case(tmp_path)
    final = store.object_directory(binding.snapshot_id)
    original_seal = module._seal
    existing_inode = None

    def create_destination_after_sealing(directory):
        nonlocal existing_inode
        original_seal(directory)
        final.mkdir()
        existing_inode = final.stat().st_ino

    monkeypatch.setattr(module, "_seal", create_destination_after_sealing)
    with store.guard(), pytest.raises((FileExistsError, ValueError)):
        store.ensure(project)
    assert final.is_dir()
    assert final.stat().st_ino == existing_inode
    assert list(final.iterdir()) == []
    assert not [
        item for item in final.parent.iterdir() if item.name != final.name
    ]
    assert list((store.root / "staging").iterdir()) == []
    assert source.is_dir()


def test_copy_fault_leaves_no_visible_object_or_staging(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module
    from tests.matlab_project_helpers import prepared_project

    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    original = module._copy_inventory

    def fail_after_copy(source, destination, manifest):
        original(source, destination, manifest)
        raise OSError("injected disk failure")

    monkeypatch.setattr(module, "_copy_inventory", fail_after_copy)
    with store.guard(), pytest.raises(OSError, match="injected"):
        store.ensure(project)
    assert not list((store.root / "objects").glob("*/*"))
    assert not list((store.root / "staging").iterdir())


def test_source_addition_during_copy_invalidates_confirmation(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module
    from tests.matlab_project_helpers import prepared_project

    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    original = module._copy_inventory

    def change_after_copy(source, destination, manifest):
        original(source, destination, manifest)
        (source / "new-entry.m").write_text("x = 1;\n")

    monkeypatch.setattr(module, "_copy_inventory", change_after_copy)
    with store.guard(), pytest.raises(ValueError, match="changed"):
        store.ensure(project)
    assert not list((store.root / "objects").glob("*/*"))


def test_different_content_confirmations_can_copy_concurrently(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module
    from tests.matlab_project_helpers import prepared_project

    left = prepared_project(tmp_path / "left")
    right = prepared_project(tmp_path / "right")
    (right / "README.md").write_text("A different confirmed project.\n")
    projects = [discover_matlab_project(str(root)) for root in (left, right)]
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    both_copying = Barrier(2)
    original = module._copy_inventory

    def coordinated_copy(source, destination, manifest):
        both_copying.wait(timeout=10)
        original(source, destination, manifest)

    def confirm(project):
        with store.guard():
            return store.ensure(project)

    monkeypatch.setattr(module, "_copy_inventory", coordinated_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, project) for project in projects]
        bindings = [future.result(timeout=15) for future in futures]
    assert bindings[0].snapshot_id != bindings[1].snapshot_id
    assert len(list((store.root / "objects").glob("*/*"))) == 2


def publish_from_process(root, project_data, gate, results):
    from flight_agent.verification.matlab_contracts import MatlabProjectDescriptor

    project = MatlabProjectDescriptor.model_validate(project_data)
    snapshots = ProjectSnapshotStore(Path(root))
    gate.wait(timeout=15)
    with snapshots.guard():
        results.put(snapshots.ensure(project).snapshot_id)


def test_spawned_processes_publish_one_complete_object(tmp_path):
    import multiprocessing

    from tests.matlab_project_helpers import prepared_project

    project = discover_matlab_project(str(prepared_project(tmp_path / "source")))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=publish_from_process,
            args=(str(store.root), project.model_dump(mode="json"), gate, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        identifiers = [results.get(timeout=20), results.get(timeout=20)]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert identifiers[0] == identifiers[1]
        assert len(list((store.root / "objects").glob("*/*"))) == 1
        assert list((store.root / "staging").iterdir()) == []
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()
