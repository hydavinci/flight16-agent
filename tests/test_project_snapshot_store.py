import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.verification.project_snapshots import ProjectSnapshotStore
from tests.snapshot_helpers import snapshot_case


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
