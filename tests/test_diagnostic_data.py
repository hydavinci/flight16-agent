import hashlib
import json
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


def series(values: list[float] | None = None, unit: str = "deg") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "time_series",
        "time_unit": "s",
        "time": [0.0, 1.0, 2.0, 3.0],
        "signals": {"angle": {"unit": unit, "values": values or [0.0, 2.0, 4.0, 6.0]}},
    }


def manifest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "Synthetic diagnostic tests only",
        "synthetic": True,
        "design": {
            "controller": {"id": "demo_controller", "version": "1", "sha256": "a" * 64},
            "plant": {"id": "demo_plant", "version": "1", "sha256": "b" * 64},
        },
        "conditions": {"purpose": "synthetic unit tests"},
        "datasets": [
            {"id": "run_a", "path": "series.json", "kind": "time_series", "source": "synthetic"}
        ],
    }


def frequency_response() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "frequency_response",
        "loop": "pitch",
        "frequency_hz": [1.0, 10.0, 100.0],
        "gain_db": [20.0, 0.0, -20.0],
        "phase_deg": [-90.0, -135.0, -225.0],
        "phase_convention": "unwrapped",
        "feedback_sign": -1,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    write_json(root / "flight-diagnosis.json", manifest())
    write_json(root / "series.json", series())
    return root


def assert_no_arrays(value: object) -> None:
    assert not isinstance(value, list)
    if isinstance(value, dict):
        for child in value.values():
            assert_no_arrays(child)


def test_inspection_records_only_registered_bytes_and_bounded_summary(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    data["conditions"]["levels"] = [1, 2]
    write_json(root / "flight-diagnosis.json", data)
    (root / "unregistered.json").symlink_to(tmp_path / "do-not-open.json")
    before = {
        path.name: path.read_bytes()
        for path in (root / "series.json", root / "flight-diagnosis.json")
    }

    project = inspect_diagnostic_project(root)

    assert project.root == str(root)
    assert {file.path for file in project.files} == {"flight-diagnosis.json", "series.json"}
    for file in project.files:
        assert file.sha256 == hashlib.sha256(before[file.path]).hexdigest()
        assert file.size == len(before[file.path])
        assert (root / file.path).read_bytes() == before[file.path]
    assert project.summary["design"] == project.manifest.design.model_dump(mode="json")
    assert project.summary["schema_version"] == "1.0"
    assert_no_arrays(project.summary)
    encoded_summary = json.dumps(project.summary)
    assert all(file.sha256 in encoded_summary for file in project.files)
    assert "time" not in project.summary
    assert "values" not in encoded_summary


@pytest.mark.parametrize("change", ["dataset", "version", "conditions", "design_digest"])
def test_project_fingerprint_tracks_data_and_registered_identity(
    tmp_path: Path, change: str
) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    before = inspect_diagnostic_project(root)
    if change == "dataset":
        write_json(root / "series.json", series([1.0, 2.0, 4.0, 6.0]))
    else:
        data = manifest()
        if change == "version":
            data["design"]["controller"]["version"] = "2"
        elif change == "design_digest":
            data["design"]["plant"]["sha256"] = "c" * 64
        else:
            data["conditions"]["condition"] = "different_synthetic_condition"
        write_json(root / "flight-diagnosis.json", data)
    assert inspect_diagnostic_project(root).fingerprint != before.fingerprint


@pytest.mark.parametrize(
    "change",
    [
        "short_time", "unordered", "repeated_time", "bad_length", "nan", "infinity",
        "string_number", "bool_number", "extra", "signal_extra", "empty_unit",
        "many_samples", "many_signals", "wrong_kind", "wrong_time_unit", "bad_name",
    ],
)
def test_inspection_rejects_malformed_series(tmp_path: Path, change: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = series()
    if change == "short_time":
        data["time"] = [0.0]
    elif change == "unordered":
        data["time"] = [0.0, 2.0, 1.0, 3.0]
    elif change == "repeated_time":
        data["time"] = [0.0, 1.0, 1.0, 3.0]
    elif change == "bad_length":
        data["signals"]["angle"]["values"] = [0.0, 1.0]
    elif change in ("nan", "infinity", "string_number", "bool_number"):
        data["signals"]["angle"]["values"][0] = {
            "nan": float("nan"), "infinity": float("inf"),
            "string_number": "0.0", "bool_number": True,
        }[change]
    elif change == "extra":
        data["code"] = "not executable"
    elif change == "signal_extra":
        data["signals"]["angle"]["expression"] = "not supported"
    elif change == "empty_unit":
        data["signals"]["angle"]["unit"] = ""
    elif change == "many_samples":
        data["time"] = [float(i) for i in range(20001)]
        data["signals"]["angle"]["values"] = [0.0] * 20001
    elif change == "many_signals":
        data["signals"] = {f"signal_{i}": {"unit": "deg", "values": [0.0] * 4} for i in range(33)}
    elif change == "wrong_kind":
        data["kind"] = "frequency_response"
    elif change == "wrong_time_unit":
        data["time_unit"] = "ms"
    else:
        data["signals"]["../angle"] = data["signals"].pop("angle")
    write_json(root / "series.json", data)
    with pytest.raises(ValueError):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize(
    "change",
    ["extra", "duplicate_ids", "many_datasets", "bool_string", "nonfinite_condition",
     "nonfinite_parameter", "string_parameter", "missing_design", "wrong_schema"],
)
def test_inspection_rejects_malformed_manifest(tmp_path: Path, change: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    if change == "extra":
        data["code"] = "not executable"
    elif change == "duplicate_ids":
        data["datasets"] *= 2
    elif change == "many_datasets":
        data["datasets"] = [{**data["datasets"][0], "id": f"run_{i}"} for i in range(17)]
    elif change == "bool_string":
        data["synthetic"] = "true"
    elif change == "nonfinite_condition":
        data["conditions"]["bad"] = float("nan")
    elif change in ("nonfinite_parameter", "string_parameter"):
        data["datasets"][0]["parameters"] = {
            "gain": float("inf") if change == "nonfinite_parameter" else "1.0"
        }
    elif change == "missing_design":
        del data["design"]
    else:
        data["schema_version"] = "2.0"
    write_json(root / "flight-diagnosis.json", data)
    with pytest.raises(ValueError):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize(
    "path",
    ["../outside.json", "/tmp/outside.json", "nested/../series.json", "nested//series.json",
     "./series.json", "nested\\series.json", "C:series.json", "series.txt",
     "series.json/", "bad\0.json"],
)
def test_registration_refuses_unsafe_paths(tmp_path: Path, path: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    data["datasets"][0]["path"] = path
    write_json(root / "flight-diagnosis.json", data)
    with pytest.raises(ValueError):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("filename", ["flight-diagnosis.json", "series.json"])
def test_each_json_file_is_capped_at_eight_mib(tmp_path: Path, filename: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    (root / filename).write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="8 MiB"):
        inspect_diagnostic_project(root)


def test_json_cap_matches_shared_decimal_evidence_limit(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    content = (root / "series.json").read_bytes()
    (root / "series.json").write_bytes(content + b" " * (8_000_001 - len(content)))
    with pytest.raises(ValueError, match="limit|8 MiB"):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("kind", ["root", "parent", "manifest", "dataset", "dataset_parent"])
def test_inspection_never_follows_symlinks(tmp_path: Path, kind: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    if kind in ("root", "parent"):
        alias = tmp_path / "alias"
        alias.symlink_to(root if kind == "root" else tmp_path, target_is_directory=True)
        root = alias if kind == "root" else alias / "project"
    elif kind == "dataset_parent":
        data = manifest()
        data["datasets"][0]["path"] = "nested/series.json"
        write_json(root / "flight-diagnosis.json", data)
        (root / "nested").symlink_to(root, target_is_directory=True)
    else:
        file = root / ("flight-diagnosis.json" if kind == "manifest" else "series.json")
        outside = tmp_path / "outside.json"
        file.rename(outside)
        file.symlink_to(outside)
    with pytest.raises(ValueError, match="safe|symlink"):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("bad_content", ['{"kind":', '{"kind":"time_series","kind":"time_series"}'])
def test_invalid_or_duplicate_key_json_is_rejected(tmp_path: Path, bad_content: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    (root / "series.json").write_text(bad_content)
    with pytest.raises(ValueError):
        inspect_diagnostic_project(root)


def test_inspection_accepts_registered_negative_feedback_response(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import (
        DiagnosticContext,
        inspect_diagnostic_project,
    )

    root = make_project(tmp_path)
    data = manifest()
    data["datasets"][0]["kind"] = "frequency_response"
    write_json(root / "flight-diagnosis.json", data)
    write_json(root / "series.json", frequency_response())
    project = inspect_diagnostic_project(root)
    response = DiagnosticContext(project).read_dataset("run_a")
    assert response.kind == "frequency_response"
    assert response.loop == "pitch"
    assert_no_arrays(project.summary)


@pytest.mark.parametrize(
    "change",
    ["short", "zero", "negative", "unordered", "repeated", "gain_length", "phase_length",
     "nan", "phase_wrapped", "positive_feedback", "float_feedback", "extra", "missing_phase"],
)
def test_inspection_rejects_malformed_frequency_response(tmp_path: Path, change: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    registration = manifest()
    registration["datasets"][0]["kind"] = "frequency_response"
    write_json(root / "flight-diagnosis.json", registration)
    data = frequency_response()
    if change == "short":
        data["frequency_hz"] = [1.0]
    elif change in ("zero", "negative", "repeated", "unordered"):
        data["frequency_hz"] = {
            "zero": [0.0, 10.0, 100.0], "negative": [-1.0, 10.0, 100.0],
            "repeated": [1.0, 1.0, 100.0], "unordered": [100.0, 10.0, 1.0],
        }[change]
    elif change in ("gain_length", "phase_length"):
        data["gain_db" if change == "gain_length" else "phase_deg"] = [1.0, 2.0]
    elif change == "nan":
        data["phase_deg"][0] = float("nan")
    elif change == "phase_wrapped":
        data["phase_convention"] = "wrapped"
    elif change == "positive_feedback":
        data["feedback_sign"] = 1
    elif change == "float_feedback":
        data["feedback_sign"] = -1.0
    elif change == "missing_phase":
        del data["phase_convention"]
    else:
        data["expression"] = "unsupported"
    write_json(root / "series.json", data)
    with pytest.raises(ValueError):
        inspect_diagnostic_project(root)


def test_snapshot_copies_registered_inputs_read_only_and_reuses_identical_content(
    tmp_path: Path,
) -> None:
    from flight_agent.verification.diagnostic_data import (
        inspect_diagnostic_project,
        snapshot_diagnostic_project,
    )

    root = make_project(tmp_path)
    data = manifest()
    data["datasets"][0]["path"] = "nested/series.json"
    write_json(root / "flight-diagnosis.json", data)
    write_json(root / "nested/series.json", series())
    (root / "unregistered.json").write_text("do not copy")
    project = inspect_diagnostic_project(root)
    source_stats = {file.path: (root / file.path).stat() for file in project.files}
    target = tmp_path / "snapshot"

    snapshot = snapshot_diagnostic_project(project, target)

    assert snapshot.root == str(target)
    assert snapshot.fingerprint == project.fingerprint
    assert not (target / "unregistered.json").exists()
    for file in project.files:
        assert (target / file.path).read_bytes() == (root / file.path).read_bytes()
        assert (target / file.path).stat().st_mode & 0o222 == 0
        assert (target / file.path).stat().st_ino != (root / file.path).stat().st_ino
        assert (root / file.path).stat().st_mode == source_stats[file.path].st_mode
        assert (root / file.path).stat().st_mtime_ns == source_stats[file.path].st_mtime_ns
    assert snapshot_diagnostic_project(project, target) == snapshot


def crash_snapshot(
    root: Path, target: Path, filename: str, termination: str = "exit",
) -> tuple[Path, Path]:
    crashed_file = target / filename
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import signal
import sys
from pathlib import Path
from flight_agent.verification import diagnostic_data as adapter

link = adapter.os.link
def crash_after_publication(*args, **kwargs):
    link(*args, **kwargs)
    if args[1] == sys.argv[3]:
        if sys.argv[4] == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(73)

adapter.os.link = crash_after_publication
adapter.snapshot_diagnostic_project(
    adapter.inspect_diagnostic_project(Path(sys.argv[1])), Path(sys.argv[2])
)
""",
            str(root),
            str(target),
            crashed_file.name,
            termination,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (-signal.SIGKILL if termination == "kill" else 73), result.stderr
    staging, = crashed_file.parent.glob(".diagnostic-*.tmp")
    receipt, = crashed_file.parent.glob(".diagnostic-*.owner")
    assert staging.samefile(crashed_file)
    assert crashed_file.stat().st_nlink == 2
    return staging, receipt


@pytest.mark.parametrize("file_number", [0, 1])
@pytest.mark.parametrize("termination", ["exit", "kill"])
def test_snapshot_recovers_after_process_exit(
    tmp_path: Path, file_number: int, termination: str,
) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    staging, receipt = crash_snapshot(root, target, project.files[file_number].path, termination)

    snapshot = adapter.snapshot_diagnostic_project(project, target)

    assert snapshot.fingerprint == project.fingerprint
    assert not staging.exists()
    assert not receipt.exists()
    assert {path.name for path in target.iterdir()} == {file.path for file in project.files}
    for file in project.files:
        copied = target / file.path
        assert copied.read_bytes() == (root / file.path).read_bytes()
        assert copied.stat().st_mode & 0o222 == 0
        assert copied.stat().st_nlink == 1
        assert copied.stat().st_ino != (root / file.path).stat().st_ino


def test_snapshot_preserves_unowned_staging_after_process_exit(tmp_path: Path) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    staging, receipt = crash_snapshot(root, target, "flight-diagnosis.json")
    foreign = target / f".diagnostic-{'0' * 32}.tmp"
    foreign.write_bytes(staging.read_bytes())
    foreign_receipt = target / ".diagnostic-unrelated.owner"
    foreign_receipt.write_text("0" * 32)
    before = {path: path.stat() for path in (foreign, foreign_receipt)}

    assert adapter.snapshot_diagnostic_project(project, target).fingerprint == project.fingerprint

    assert not staging.exists()
    assert not receipt.exists()
    assert foreign.read_bytes() == (root / "flight-diagnosis.json").read_bytes()
    assert foreign_receipt.read_text() == "0" * 32
    for path, expected in before.items():
        actual = path.stat()
        assert (actual.st_ino, actual.st_mode, actual.st_nlink, actual.st_mtime_ns) == (
            expected.st_ino, expected.st_mode, expected.st_nlink, expected.st_mtime_ns
        )


@pytest.mark.parametrize("alteration", [
    "external_link", "missing_receipt", "invalid_receipt", "receipt_symlink",
    "receipt_hardlink", "writable_receipt", "staging_symlink", "wrong_staging",
    "other_directory", "different_content", "destination_symlink",
])
def test_snapshot_refuses_unsafe_orphan_recovery(tmp_path: Path, alteration: str) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    staging, receipt = crash_snapshot(root, target, "flight-diagnosis.json")
    destination = target / "flight-diagnosis.json"
    outside = tmp_path / "outside"
    if alteration == "external_link":
        outside.hardlink_to(destination)
    elif alteration == "missing_receipt":
        receipt.unlink()
    elif alteration == "invalid_receipt":
        receipt.chmod(0o600)
        receipt.write_bytes(b"." * 32)
        receipt.chmod(0o444)
    elif alteration == "receipt_symlink":
        receipt.rename(outside)
        receipt.symlink_to(outside)
    elif alteration == "receipt_hardlink":
        outside.hardlink_to(receipt)
    elif alteration == "writable_receipt":
        receipt.chmod(0o600)
    elif alteration in ("staging_symlink", "wrong_staging"):
        outside.hardlink_to(destination)
        staging.unlink()
        if alteration == "staging_symlink":
            staging.symlink_to(outside)
        else:
            staging.write_bytes(destination.read_bytes())
            staging.chmod(0o444)
    elif alteration == "other_directory":
        moved = tmp_path / "other_snapshot"
        moved.mkdir()
        for path in target.iterdir():
            path.rename(moved / path.name)
        target = moved
    elif alteration == "different_content":
        destination.chmod(0o600)
        destination.write_bytes(b"incomplete or changed snapshot")
        destination.chmod(0o444)
    elif alteration == "destination_symlink":
        destination.unlink()
        destination.symlink_to(staging)
    paths = [*target.iterdir(), *(root / file.path for file in project.files)]
    if outside.exists():
        paths.append(outside)
    before = {
        path: (path.lstat(), path.readlink() if path.is_symlink() else path.read_bytes())
        for path in paths
    }

    with pytest.raises(ValueError):
        adapter.snapshot_diagnostic_project(project, target)

    assert set(target.iterdir()) == {path for path in paths if path.parent == target}
    for path, (expected, content) in before.items():
        actual = path.lstat()
        assert (actual.st_ino, actual.st_mode, actual.st_nlink, actual.st_mtime_ns) == (
            expected.st_ino, expected.st_mode, expected.st_nlink, expected.st_mtime_ns
        )
        assert (path.readlink() if path.is_symlink() else path.read_bytes()) == content


@pytest.mark.parametrize("cleanup", ["staging", "receipt"])
def test_snapshot_recovers_after_exit_during_orphan_cleanup(
    tmp_path: Path, cleanup: str,
) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    staging, receipt = crash_snapshot(root, target, "flight-diagnosis.json")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from pathlib import Path
from flight_agent.verification import diagnostic_data as adapter

unlink = adapter.os.unlink
def crash_during_recovery(path, **kwargs):
    if path == sys.argv[3]:
        os._exit(73)
    unlink(path, **kwargs)

adapter.os.unlink = crash_during_recovery
adapter.snapshot_diagnostic_project(
    adapter.inspect_diagnostic_project(Path(sys.argv[1])), Path(sys.argv[2])
)
""",
            str(root),
            str(target),
            (staging if cleanup == "staging" else receipt).name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    unpublished = set(target.glob(".diagnostic-*.tmp")) - {staging}
    assert len(unpublished) == 1

    assert adapter.snapshot_diagnostic_project(project, target).fingerprint == project.fingerprint

    assert not staging.exists()
    assert all(path.exists() and path.stat().st_nlink == 1 for path in unpublished)
    for file in project.files:
        copied = target / file.path
        assert copied.stat().st_nlink == 1
        assert copied.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("write_number", [1, 2])
@pytest.mark.parametrize("interruption", [OSError, KeyboardInterrupt])
def test_snapshot_recovers_after_interrupted_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_number: int,
    interruption: type[BaseException],
) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    target.mkdir()
    foreign = target / ".unrelated-staging.tmp"
    foreign.write_bytes(b"preserve")
    fdopen = adapter.os.fdopen
    writes = 0

    @contextmanager
    def interrupted_open(fd: int, mode: str) -> Iterator[Any]:
        nonlocal writes
        with fdopen(fd, mode) as stream:
            if mode == "wb":
                writes += 1
                if writes == write_number:
                    stream.write(b"partial snapshot")
                    stream.flush()
                    raise interruption("injected write interruption")
            yield stream

    with monkeypatch.context() as patch:
        patch.setattr(adapter.os, "fdopen", interrupted_open)
        with pytest.raises(ValueError if interruption is OSError else interruption):
            adapter.snapshot_diagnostic_project(project, target)

    remaining = {path.name for path in target.iterdir()}
    snapshot = adapter.snapshot_diagnostic_project(project, target)
    assert remaining == (
        {foreign.name} if write_number == 1 else {foreign.name, "flight-diagnosis.json"}
    )
    assert snapshot.fingerprint == project.fingerprint
    assert foreign.read_bytes() == b"preserve"
    assert {path.name for path in target.iterdir()} == (
        {foreign.name} | {file.path for file in project.files}
    )
    for file in project.files:
        copied = target / file.path
        assert copied.read_bytes() == (root / file.path).read_bytes()
        assert copied.stat().st_mode & 0o222 == 0
        assert copied.stat().st_nlink == 1
        assert copied.stat().st_ino != (root / file.path).stat().st_ino


@pytest.mark.parametrize("interruption", ["sync", "before_publication", "after_publication"])
def test_snapshot_recovers_from_publication_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    link, fsync = adapter.os.link, adapter.os.fsync
    synced = False

    def interrupted_sync(fd: int) -> None:
        nonlocal synced
        if interruption == "sync":
            raise KeyboardInterrupt("injected sync interruption")
        fsync(fd)
        synced = True

    def interrupted_link(source: str, destination: str, **kwargs: Any) -> None:
        assert synced
        assert (target / source).read_bytes() == (root / destination).read_bytes()
        assert (target / source).stat().st_mode & 0o222 == 0
        if interruption == "after_publication":
            link(source, destination, **kwargs)
        raise KeyboardInterrupt("injected publication interruption")

    with monkeypatch.context() as patch:
        patch.setattr(adapter.os, "fsync", interrupted_sync)
        patch.setattr(adapter.os, "link", interrupted_link)
        with pytest.raises(KeyboardInterrupt, match="injected"):
            adapter.snapshot_diagnostic_project(project, target)

    assert {path.name for path in target.iterdir()} == (
        {"flight-diagnosis.json"} if interruption == "after_publication" else set()
    )
    if interruption == "after_publication":
        assert (target / "flight-diagnosis.json").stat().st_nlink == 1
    assert adapter.snapshot_diagnostic_project(project, target).fingerprint == project.fingerprint


@pytest.mark.parametrize("identical", [False, True])
def test_snapshot_preserves_destination_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identical: bool,
) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    root = make_project(tmp_path)
    project = adapter.inspect_diagnostic_project(root)
    target = tmp_path / "snapshot"
    link = adapter.os.link
    competing: dict[str, bytes] = {}

    def competing_link(source: str, destination: str, **kwargs: Any) -> None:
        content = (root / destination).read_bytes() if identical else b"competing content"
        (target / destination).write_bytes(content)
        competing[destination] = content
        link(source, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(adapter.os, "link", competing_link)
        if identical:
            snapshot = adapter.snapshot_diagnostic_project(project, target)
            assert snapshot.fingerprint == project.fingerprint
        else:
            with pytest.raises(ValueError, match="different content"):
                adapter.snapshot_diagnostic_project(project, target)

    assert competing
    assert {path.name for path in target.iterdir()} == set(competing)
    for path, content in competing.items():
        assert (target / path).read_bytes() == content
        assert (target / path).stat().st_nlink == 1


@pytest.mark.parametrize("suffix", [".tmp", ".owner"])
def test_snapshot_does_not_clean_a_staging_file_it_did_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    from flight_agent.verification import diagnostic_data as adapter

    project = adapter.inspect_diagnostic_project(make_project(tmp_path))
    target = tmp_path / "snapshot"
    open_file = adapter.os.open
    competing: list[str] = []

    def collide(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None,
    ) -> int:
        if flags & adapter.os.O_EXCL and path.endswith(suffix):
            fd = open_file(path, flags, mode, dir_fd=dir_fd)
            with adapter.os.fdopen(fd, "wb") as stream:
                stream.write(b"foreign staging content")
            competing.append(path)
        return open_file(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(adapter.os, "open", collide)
        with pytest.raises(ValueError):
            adapter.snapshot_diagnostic_project(project, target)

    assert len(competing) == 1
    assert {path.name for path in target.iterdir()} == set(competing)
    assert (target / competing[0]).read_bytes() == b"foreign staging content"


def test_snapshot_does_not_replace_differing_existing_content(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import (
        inspect_diagnostic_project,
        snapshot_diagnostic_project,
    )

    project = inspect_diagnostic_project(make_project(tmp_path))
    target = tmp_path / "snapshot"
    target.mkdir()
    (target / "series.json").write_text("different existing content")
    with pytest.raises(ValueError, match="different|differ"):
        snapshot_diagnostic_project(project, target)
    assert (target / "series.json").read_text() == "different existing content"


@pytest.mark.parametrize("changed", ["series.json", "flight-diagnosis.json"])
def test_changed_source_is_rejected_by_snapshot_and_context(tmp_path: Path, changed: str) -> None:
    from flight_agent.verification.diagnostic_data import (
        DiagnosticContext,
        inspect_diagnostic_project,
        snapshot_diagnostic_project,
    )

    root = make_project(tmp_path)
    project = inspect_diagnostic_project(root)
    context = DiagnosticContext(project)
    (root / changed).write_bytes((root / changed).read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed|digest|identity"):
        snapshot_diagnostic_project(project, tmp_path / "snapshot")
    with pytest.raises(ValueError, match="changed|digest|identity"):
        context.read_dataset("run_a")


def test_context_rejects_forged_or_mutated_descriptor(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import (
        DiagnosticContext,
        inspect_diagnostic_project,
    )

    project = inspect_diagnostic_project(make_project(tmp_path))
    with pytest.raises(ValueError, match="identity|digest"):
        DiagnosticContext(project.model_copy(update={"fingerprint": "f" * 64}))
    context = DiagnosticContext(project)
    context.project.manifest.conditions["tampered"] = True
    with pytest.raises(ValueError, match="changed|identity"):
        context.read_dataset("run_a")


@pytest.mark.parametrize("target_kind", ["same", "inside", "ancestor", "traversal", "double_root"])
def test_snapshot_refuses_overlapping_or_traversing_targets(
    tmp_path: Path, target_kind: str
) -> None:
    from flight_agent.verification.diagnostic_data import (
        inspect_diagnostic_project,
        snapshot_diagnostic_project,
    )

    root = make_project(tmp_path)
    target = {
        "same": root, "inside": root / "snapshot", "ancestor": tmp_path,
        "traversal": tmp_path / "unused" / ".." / "snapshot",
        "double_root": Path("/" + str(root / "snapshot")),
    }[target_kind]
    with pytest.raises(ValueError):
        snapshot_diagnostic_project(inspect_diagnostic_project(root), target)
    assert not (root / "snapshot").exists()
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("kind", ["root", "parent", "file", "nested", "hardlink"])
def test_snapshot_rejects_unsafe_destinations_without_outside_writes(
    tmp_path: Path, kind: str
) -> None:
    from flight_agent.verification.diagnostic_data import (
        inspect_diagnostic_project,
        snapshot_diagnostic_project,
    )

    root = make_project(tmp_path)
    data = manifest()
    data["datasets"][0]["path"] = "nested/series.json"
    write_json(root / "flight-diagnosis.json", data)
    write_json(root / "nested/series.json", series())
    project = inspect_diagnostic_project(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    sentinel.write_text("unchanged")
    target = tmp_path / "snapshot"
    if kind in ("root", "parent"):
        target.symlink_to(outside, target_is_directory=True)
        if kind == "parent":
            target = target / "child"
    else:
        target.mkdir()
        if kind == "file":
            (target / "flight-diagnosis.json").symlink_to(sentinel)
        elif kind == "nested":
            (target / "nested").symlink_to(outside, target_is_directory=True)
        else:
            (target / "flight-diagnosis.json").hardlink_to(root / "flight-diagnosis.json")
    original_mode = (root / "flight-diagnosis.json").stat().st_mode
    with pytest.raises(ValueError):
        snapshot_diagnostic_project(project, target)
    assert sentinel.read_text() == "unchanged"
    assert set(outside.iterdir()) == {sentinel}
    assert (root / "flight-diagnosis.json").stat().st_mode == original_mode


def invoke(root: Path, name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    from flight_agent.runtime.contracts import ExecutionScope
    from flight_agent.verification.diagnostic_data import (
        DiagnosticContext,
        diagnostic_capabilities,
        inspect_diagnostic_project,
    )

    project = inspect_diagnostic_project(root)
    scope = ExecutionScope(baseline_version=1, confirmed=True, design=project.manifest.design)
    return diagnostic_capabilities().invoke(name, inputs, DiagnosticContext(project), scope)


def add_series(
    root: Path, name: str, values: list[float], *, times: list[float] | None = None,
    unit: str = "deg", parameter: float | None = None,
) -> None:
    data = json.loads((root / "flight-diagnosis.json").read_bytes())
    data["datasets"].append({
        "id": name, "path": f"{name}.json", "kind": "time_series", "source": "synthetic",
        "parameters": {} if parameter is None else {"gain": parameter},
    })
    write_json(root / "flight-diagnosis.json", data)
    content = series(values, unit)
    if times is not None:
        content["time"] = times
    write_json(root / f"{name}.json", content)


def window_inputs(**changes: object) -> dict[str, Any]:
    return {"dataset": "run_a", "signal": "angle", "start": 0.0, "end": 3.0, **changes}


def comparison_inputs(**changes: object) -> dict[str, Any]:
    return {
        "observed_dataset": "run_a", "observed_signal": "angle",
        "reference_dataset": "run_b", "reference_signal": "angle",
        "start": 0.0, "end": 3.0, "alignment": "linear_overlap",
        "reference_time_offset": 0.0, **changes,
    }


def sensitivity_inputs(**changes: object) -> dict[str, Any]:
    return {
        "datasets": ["run_a", "run_b", "run_c"], "signal": "angle", "parameter": "gain",
        "start": 0.0, "end": 3.0, **changes,
    }


def loop_inputs(**changes: object) -> dict[str, Any]:
    return {"dataset": "run_a", "loop": "pitch", "start_hz": 1.0, "end_hz": 100.0, **changes}


def make_frequency_project(tmp_path: Path, content: dict[str, Any] | None = None) -> Path:
    root = make_project(tmp_path)
    registration = manifest()
    registration["datasets"][0]["kind"] = "frequency_response"
    write_json(root / "flight-diagnosis.json", registration)
    write_json(root / "series.json", frequency_response() if content is None else content)
    return root


def make_sensitivity_project(tmp_path: Path) -> Path:
    root = make_project(tmp_path)
    registration = manifest()
    registration["datasets"][0]["parameters"] = {"gain": 1.0}
    write_json(root / "flight-diagnosis.json", registration)
    write_json(root / "series.json", series([3.0] * 4))
    add_series(root, "run_b", [5.0] * 4, parameter=2.0)
    add_series(root, "run_c", [7.0] * 4, parameter=3.0)
    return root


def test_catalog_exposes_only_four_strict_version_one_read_only_capabilities() -> None:
    from flight_agent.verification.diagnostic_data import diagnostic_capabilities

    catalog = diagnostic_capabilities().catalog()
    assert {item["name"] for item in catalog} == {
        "window_statistics", "compare_signals", "parameter_sensitivity", "local_loop",
    }
    for item in catalog:
        assert item["version"] == "1"
        assert item["effect"] == "read_only"
        assert isinstance(item["input_schema"], dict)
        assert item["input_schema"]["additionalProperties"] is False


def test_window_statistics_are_inclusive_and_match_analytic_values(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    result = invoke(root, "window_statistics", window_inputs(start=1.0, end=2.0))
    assert result["valid"] is True
    assert result["unknowns"] == []
    expected = {"count": 2.0, "mean": 3.0, "minimum": 2.0, "maximum": 4.0,
                "rms": 10.0 ** 0.5, "range": 2.0}
    for name, value in expected.items():
        assert result["metrics"][name]["value"] == pytest.approx(value)
        assert result["metrics"][name]["unit"] == ("samples" if name == "count" else "deg")
    assert result["data_fingerprints"] == {
        "run_a": hashlib.sha256((root / "series.json").read_bytes()).hexdigest()
    }


@pytest.mark.parametrize("start,end", [(-1.0, 3.0), (0.0, 4.0), (0.1, 0.2), (0.5, 1.5)])
def test_statistics_report_insufficient_window_evidence(
    tmp_path: Path, start: float, end: float
) -> None:
    result = invoke(
        make_project(tmp_path), "window_statistics", window_inputs(start=start, end=end)
    )
    assert result["valid"] is False
    assert result["metrics"] == {}
    assert result["unknowns"]


def test_missing_signal_is_unknown_not_a_numeric_result(tmp_path: Path) -> None:
    result = invoke(
        make_project(tmp_path), "window_statistics", window_inputs(signal="missing")
    )
    assert result["valid"] is False
    assert result["metrics"] == {}
    assert "missing" in " ".join(result["unknowns"]).lower()


def test_comparison_applies_explicit_offset_and_linear_reference_interpolation(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    write_json(root / "series.json", series([1.0, 3.0, 5.0, 7.0]))
    add_series(root, "run_b", [0.0, 4.0, 8.0], times=[-1.0, 1.0, 3.0])
    result = invoke(root, "compare_signals", comparison_inputs(reference_time_offset=1.0))
    assert result["valid"] is True
    for name in ("rmse", "mean_error", "max_abs_error"):
        assert result["metrics"][name] == {"value": 1.0, "unit": "deg"}
    assert result["metrics"]["count"] == {"value": 4.0, "unit": "samples"}
    assert result["metrics"]["correlation"]["value"] == pytest.approx(1.0)
    assert set(result["data_fingerprints"]) == {"run_a", "run_b"}
    unshifted = invoke(root, "compare_signals", comparison_inputs())
    assert unshifted["metrics"]["mean_error"]["value"] == pytest.approx(-1.0)


def test_comparison_uses_only_actual_overlap_never_extrapolation(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    add_series(root, "run_b", [1.0, 2.0], times=[1.0, 2.0])
    result = invoke(root, "compare_signals", comparison_inputs())
    assert result["valid"] is True
    assert result["metrics"]["count"]["value"] == 2.0
    assert result["metrics"]["mean_error"]["value"] == pytest.approx(1.5)
    assert result["metrics"]["rmse"]["value"] == pytest.approx(2.5 ** 0.5)


@pytest.mark.parametrize("problem", ["no_overlap", "one_pair", "units", "signal"])
def test_comparison_explicitly_rejects_insufficient_or_incompatible_evidence(
    tmp_path: Path, problem: str
) -> None:
    root = make_project(tmp_path)
    times = {"no_overlap": [4.0, 5.0], "one_pair": [2.5, 4.0]}.get(problem, [0.0, 3.0])
    add_series(root, "run_b", [0.0, 6.0], times=times, unit="rad" if problem == "units" else "deg")
    inputs = (
        comparison_inputs(reference_signal="missing")
        if problem == "signal" else comparison_inputs()
    )
    result = invoke(root, "compare_signals", inputs)
    assert result["valid"] is False
    assert result["metrics"] == {}
    assert result["unknowns"]


def test_constant_signal_correlation_is_unknown_without_invalidating_errors(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    add_series(root, "run_b", [2.0] * 4)
    result = invoke(root, "compare_signals", comparison_inputs())
    assert result["valid"] is True
    assert "correlation" not in result["metrics"]
    assert "constant" in " ".join(result["unknowns"]).lower()
    assert result["metrics"]["mean_error"]["value"] == pytest.approx(1.0)


def test_parameter_sensitivity_regresses_window_means_with_units(tmp_path: Path) -> None:
    result = invoke(
        make_sensitivity_project(tmp_path), "parameter_sensitivity", sensitivity_inputs()
    )
    assert result["valid"] is True
    assert result["metrics"]["gradient"]["value"] == pytest.approx(2.0)
    assert result["metrics"]["gradient"]["unit"] == "deg/gain"
    assert result["metrics"]["r_squared"]["value"] == pytest.approx(1.0)
    assert result["metrics"]["count"] == {"value": 3.0, "unit": "datasets"}
    assert set(result["data_fingerprints"]) == {"run_a", "run_b", "run_c"}
    assert "not proof of causation" in " ".join(result["observations"]).lower()


@pytest.mark.parametrize("problem", ["metadata", "units", "same_parameter", "signal", "window"])
def test_parameter_sensitivity_requires_all_requested_evidence(
    tmp_path: Path, problem: str
) -> None:
    root = make_sensitivity_project(tmp_path)
    inputs = sensitivity_inputs()
    if problem in ("metadata", "same_parameter"):
        registration = json.loads((root / "flight-diagnosis.json").read_bytes())
        if problem == "metadata":
            registration["datasets"][1]["parameters"] = {}
        else:
            for dataset in registration["datasets"]:
                dataset["parameters"]["gain"] = 1.0
        write_json(root / "flight-diagnosis.json", registration)
    elif problem == "units":
        write_json(root / "run_b.json", series([5.0] * 4, unit="rad"))
    elif problem == "signal":
        inputs["signal"] = "missing"
    else:
        inputs["end"] = 4.0
    result = invoke(root, "parameter_sensitivity", inputs)
    assert result["valid"] is False
    assert result["metrics"] == {}
    assert result["unknowns"]


def test_constant_sensitivity_response_has_gradient_but_undefined_fit_quality(
    tmp_path: Path,
) -> None:
    root = make_sensitivity_project(tmp_path)
    write_json(root / "run_b.json", series([3.0] * 4))
    write_json(root / "run_c.json", series([3.0] * 4))
    result = invoke(root, "parameter_sensitivity", sensitivity_inputs())
    assert result["valid"] is True
    assert result["metrics"]["gradient"]["value"] == 0.0
    assert "r_squared" not in result["metrics"]
    assert result["unknowns"]


def test_zero_gradient_survives_extreme_parameter_and_response_scales(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    registration = manifest()
    registration["datasets"][0]["parameters"] = {"gain": 1e-308}
    write_json(root / "flight-diagnosis.json", registration)
    write_json(root / "series.json", series([1e308] * 4))
    add_series(root, "run_b", [1e308] * 4, parameter=2e-308)
    result = invoke(
        root, "parameter_sensitivity", sensitivity_inputs(datasets=["run_a", "run_b"])
    )
    assert result["valid"] is True
    assert result["metrics"]["gradient"]["value"] == 0.0
    assert "r_squared" not in result["metrics"]


def test_finite_gradient_is_not_lost_to_intermediate_scale_overflow(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    registration = manifest()
    registration["datasets"][0]["parameters"] = {"gain": 0.0}
    write_json(root / "flight-diagnosis.json", registration)
    write_json(root / "series.json", series([1e308] * 4))
    add_series(root, "run_b", [1.5e308] * 4, parameter=0.5)
    result = invoke(
        root, "parameter_sensitivity", sensitivity_inputs(datasets=["run_a", "run_b"])
    )
    assert result["valid"] is True
    assert result["metrics"]["gradient"]["value"] == pytest.approx(1e308)


@pytest.mark.parametrize("phase_offset", [0.0, -360.0, 720.0])
def test_local_loop_interpolates_unwrapped_crossings_in_log_frequency(
    tmp_path: Path, phase_offset: float
) -> None:
    data = frequency_response()
    data["phase_deg"] = [value + phase_offset for value in data["phase_deg"]]
    result = invoke(make_frequency_project(tmp_path, data), "local_loop", loop_inputs())
    assert result["valid"] is True
    expected = {
        "phase_margin_deg": (45.0, "deg"),
        "gain_margin_db": (10.0, "dB"),
        "gain_crossover_hz": (10.0, "Hz"),
        "phase_crossover_hz": (1000.0 ** 0.5, "Hz"),
    }
    for name, (value, unit) in expected.items():
        assert result["metrics"][name]["value"] == pytest.approx(value)
        assert result["metrics"][name]["unit"] == unit
    assert "complete closed-loop stability" in " ".join(result["observations"]).lower()


def test_local_loop_selects_smallest_absolute_margin_deterministically(tmp_path: Path) -> None:
    data = frequency_response()
    data["gain_db"] = [10.0, -10.0, 10.0]
    data["phase_deg"] = [-100.0, -160.0, -220.0]
    result = invoke(make_frequency_project(tmp_path, data), "local_loop", loop_inputs())
    assert result["valid"] is True
    assert result["metrics"]["phase_margin_deg"]["value"] == pytest.approx(-10.0)
    assert result["metrics"]["gain_crossover_hz"]["value"] == pytest.approx(1000.0 ** 0.5)
    assert result["metrics"]["gain_margin_db"]["value"] == pytest.approx(10.0 / 3.0)
    assert "smallest absolute" in " ".join(result["observations"]).lower()


def test_local_loop_selects_smallest_absolute_gain_margin(tmp_path: Path) -> None:
    data = frequency_response()
    data["frequency_hz"] = [1.0, 10.0, 100.0, 1000.0]
    data["gain_db"] = [20.0, 10.0, -10.0, -30.0]
    data["phase_deg"] = [-90.0, -270.0, -450.0, -630.0]
    result = invoke(
        make_frequency_project(tmp_path, data), "local_loop", loop_inputs(end_hz=1000.0)
    )
    assert result["valid"] is True
    assert result["metrics"]["gain_margin_db"]["value"] == pytest.approx(-15.0)
    assert result["metrics"]["phase_crossover_hz"]["value"] == pytest.approx(10.0 ** 0.5)


def test_local_loop_without_crossings_does_not_invent_infinite_margins(tmp_path: Path) -> None:
    data = frequency_response()
    data["gain_db"] = [-1.0, -2.0, -3.0]
    data["phase_deg"] = [-20.0, -30.0, -40.0]
    result = invoke(make_frequency_project(tmp_path, data), "local_loop", loop_inputs())
    assert result["valid"] is False
    assert result["metrics"] == {}
    assert len(result["unknowns"]) >= 2
    assert "complete closed-loop stability" in " ".join(result["observations"]).lower()
    json.dumps(result, allow_nan=False)


def test_local_loop_partial_evidence_retains_only_determinable_margin(tmp_path: Path) -> None:
    data = frequency_response()
    data["phase_deg"] = [-90.0, -135.0, -150.0]
    result = invoke(make_frequency_project(tmp_path, data), "local_loop", loop_inputs())
    assert result["valid"] is False
    assert result["metrics"]["phase_margin_deg"]["value"] == 45.0
    assert "gain_margin_db" not in result["metrics"]
    assert result["unknowns"]


@pytest.mark.parametrize("plateau", ["gain_db", "phase_deg"])
def test_nonisolated_crossing_plateaus_are_explicit_unknowns(tmp_path: Path, plateau: str) -> None:
    data = frequency_response()
    data[plateau] = [0.0 if plateau == "gain_db" else -180.0] * 3
    result = invoke(make_frequency_project(tmp_path, data), "local_loop", loop_inputs())
    assert result["valid"] is False
    assert "plateau" in " ".join(result["unknowns"]).lower()


def test_local_loop_requires_matching_registered_loop_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loop|identity"):
        invoke(make_frequency_project(tmp_path), "local_loop", loop_inputs(loop="roll"))


def test_local_loop_does_not_extrapolate_beyond_recorded_band(tmp_path: Path) -> None:
    result = invoke(
        make_frequency_project(tmp_path), "local_loop", loop_inputs(start_hz=0.1, end_hz=1000.0)
    )
    assert result["valid"] is False
    assert result["metrics"] == {}
    assert result["unknowns"]


@pytest.mark.parametrize(
    "name,inputs",
    [
        ("window_statistics", window_inputs(start=3.0, end=3.0)),
        ("window_statistics", window_inputs(start=True)),
        ("window_statistics", window_inputs(start="0")),
        ("window_statistics", window_inputs(end=float("inf"))),
        ("window_statistics", window_inputs(expression="unsupported")),
        ("compare_signals", comparison_inputs(alignment="automatic")),
        ("compare_signals", comparison_inputs(reference_time_offset=float("nan"))),
        ("parameter_sensitivity", sensitivity_inputs(datasets=["run_a"])),
        ("parameter_sensitivity", sensitivity_inputs(datasets=["run_a", "run_a"])),
        ("parameter_sensitivity", sensitivity_inputs(datasets=[f"run_{i}" for i in range(17)])),
        ("local_loop", loop_inputs(start_hz=0.0)),
        ("local_loop", loop_inputs(start_hz=100.0, end_hz=1.0)),
    ],
)
def test_capability_inputs_are_strict_and_bounded(name: str, inputs: dict[str, Any]) -> None:
    from flight_agent.verification.diagnostic_data import diagnostic_capabilities

    with pytest.raises(ValueError):
        diagnostic_capabilities().validate_inputs(name, inputs)


def test_comparison_requires_explicit_alignment_and_offset() -> None:
    from flight_agent.verification.diagnostic_data import diagnostic_capabilities

    for required in ("alignment", "reference_time_offset"):
        inputs = comparison_inputs()
        del inputs[required]
        with pytest.raises(ValueError):
            diagnostic_capabilities().validate_inputs("compare_signals", inputs)


def test_invocation_revalidates_input_digest_after_context_creation(tmp_path: Path) -> None:
    from flight_agent.runtime.contracts import ExecutionScope
    from flight_agent.verification.diagnostic_data import (
        DiagnosticContext,
        diagnostic_capabilities,
        inspect_diagnostic_project,
    )

    root = make_project(tmp_path)
    project = inspect_diagnostic_project(root)
    context = DiagnosticContext(project)
    write_json(root / "series.json", series([9.0] * 4))
    scope = ExecutionScope(baseline_version=1, confirmed=True, design=project.manifest.design)
    with pytest.raises(ValueError, match="digest|changed"):
        diagnostic_capabilities().invoke("window_statistics", window_inputs(), context, scope)


def test_large_finite_samples_do_not_overflow_mean_or_rms(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    write_json(root / "series.json", series([1e308] * 4))
    result = invoke(root, "window_statistics", window_inputs())
    assert result["valid"] is True
    assert result["metrics"]["mean"]["value"] == pytest.approx(1e308)
    assert result["metrics"]["rms"]["value"] == pytest.approx(1e308)
    json.dumps(result, allow_nan=False)


def test_unrepresentable_error_is_unknown_never_nonfinite(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    write_json(root / "series.json", series([1e308] * 4))
    add_series(root, "run_b", [-1e308] * 4)
    result = invoke(root, "compare_signals", comparison_inputs())
    assert result["valid"] is False
    assert result["unknowns"]
    json.dumps(result, allow_nan=False)


def test_extreme_phase_winding_has_bounded_work_and_explicit_unknown(tmp_path: Path) -> None:
    data = frequency_response()
    data["phase_deg"] = [-1e100, 0.0, 1e100]
    result = invoke(make_frequency_project(tmp_path, data), "local_loop", loop_inputs())
    assert result["valid"] is False
    assert result["unknowns"]
    json.dumps(result, allow_nan=False)


def test_output_contract_rejects_nonfinite_metrics_and_unbounded_observations() -> None:
    from flight_agent.verification.diagnostic_data import DiagnosticOutput

    for metrics, observations in [
        ({"mean": {"value": float("nan"), "unit": "deg"}}, []),
        ({}, ["x" * 513]),
    ]:
        with pytest.raises(ValueError):
            DiagnosticOutput.model_validate({
                "valid": True, "metrics": metrics, "observations": observations,
                "unknowns": [], "data_fingerprints": {},
            })


def test_checked_in_fixture_is_synthetic_and_runs_without_matlab() -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = Path(__file__).resolve().parents[1] / "examples/diagnosis/synthetic-read-only"
    project = inspect_diagnostic_project(root)
    assert project.manifest.synthetic is True
    assert all(dataset.source == "synthetic" for dataset in project.manifest.datasets)
    statistics = invoke(root, "window_statistics", window_inputs(dataset="synthetic_series"))
    assert statistics["metrics"]["mean"]["value"] == 3.0
    margins = invoke(root, "local_loop", loop_inputs(dataset="synthetic_loop"))
    assert margins["metrics"]["phase_margin_deg"]["value"] == 45.0


def test_manifest_name_is_limited_to_200_characters(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    data["name"] = "名" * 200
    write_json(root / "flight-diagnosis.json", data)
    project = inspect_diagnostic_project(root)
    assert project.manifest.name == project.summary["name"] == data["name"]
    data["name"] += "x"
    write_json(root / "flight-diagnosis.json", data)
    with pytest.raises(ValueError, match="name"):
        inspect_diagnostic_project(root)


def test_signal_units_are_limited_to_80_characters(tmp_path: Path) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    unit = "°" * 80
    write_json(root / "series.json", series(unit=unit))
    result = invoke(root, "window_statistics", window_inputs())
    assert result["metrics"]["mean"]["unit"] == unit
    write_json(root / "series.json", series(unit=unit + "x"))
    with pytest.raises(ValueError, match="unit"):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("invalid", [
    {f"p{i}": float(i) for i in range(33)}, {"p" * 65: 0.0}, {"": 0.0},
])
def test_dataset_parameter_metadata_is_bounded(
    tmp_path: Path, invalid: dict[str, float],
) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    parameters = {"p" * 64: 1.0, **{f"p{i}": float(i) for i in range(31)}}
    data["datasets"][0]["parameters"] = parameters
    write_json(root / "flight-diagnosis.json", data)
    project = inspect_diagnostic_project(root)
    assert project.manifest.datasets[0].parameters == parameters
    data["datasets"][0]["parameters"] = invalid
    write_json(root / "flight-diagnosis.json", data)
    with pytest.raises(ValueError, match="parameters"):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("character", ["x", "é"])
@pytest.mark.parametrize("in_key", [False, True])
def test_conditions_canonical_utf8_limit(
    tmp_path: Path, character: str, in_key: bool,
) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    count, remainder = divmod(16_000 - 7, len(character.encode("utf-8")))
    payload = character * count + "x" * remainder
    conditions = {payload: ""} if in_key else {"": payload}
    encoded = json.dumps(
        conditions, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    assert len(encoded) == 16_000
    data["conditions"] = conditions
    write_json(root / "flight-diagnosis.json", data)
    assert inspect_diagnostic_project(root).manifest.conditions == conditions
    data["conditions"] = {payload + "x": ""} if in_key else {"": payload + "x"}
    write_json(root / "flight-diagnosis.json", data)
    with pytest.raises(ValueError, match="16,000"):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("container", ["object", "array"])
def test_conditions_allow_eight_container_levels_including_root(
    tmp_path: Path, container: str,
) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    data = manifest()
    nested: Any = 1.0
    for _ in range(7):
        nested = {"item": nested} if container == "object" else [nested]
    data["conditions"] = {"nested": nested}
    write_json(root / "flight-diagnosis.json", data)
    assert inspect_diagnostic_project(root).manifest.conditions == data["conditions"]
    data["conditions"]["nested"] = {"item": nested} if container == "object" else [nested]
    write_json(root / "flight-diagnosis.json", data)
    with pytest.raises(ValueError, match="8"):
        inspect_diagnostic_project(root)


@pytest.mark.parametrize("filename", ["flight-diagnosis.json", "series.json"])
def test_deep_json_is_an_explicit_value_error(tmp_path: Path, filename: str) -> None:
    from flight_agent.verification.diagnostic_data import inspect_diagnostic_project

    root = make_project(tmp_path)
    (root / filename).write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
    with pytest.raises(ValueError):
        inspect_diagnostic_project(root)


def test_deep_python_conditions_do_not_leak_recursion_error() -> None:
    from flight_agent.verification.diagnostic_data import DiagnosticManifest

    data = manifest()
    nested: dict[str, Any] = {}
    for _ in range(2000):
        nested = {"nested": nested}
    data["conditions"] = nested
    with pytest.raises(ValueError):
        DiagnosticManifest.model_validate(data)


@pytest.mark.parametrize("invalid", [
    {f"m{i}": {"value": float(i), "unit": "1"} for i in range(65)},
    {"m" * 65: {"value": 1.0, "unit": "1"}},
    {"": {"value": 1.0, "unit": "1"}},
    {"mean": {"value": 1.0, "unit": "u" * 81}},
])
def test_output_metric_metadata_is_bounded(invalid: dict[str, Any]) -> None:
    from flight_agent.verification.diagnostic_data import DiagnosticOutput

    metrics = {
        "m" * 64: {"value": 1.0, "unit": "u" * 80},
        **{f"m{i}": {"value": float(i), "unit": "1"} for i in range(63)},
    }
    data = {
        "valid": True, "metrics": metrics, "observations": [], "unknowns": [],
        "data_fingerprints": {},
    }
    assert DiagnosticOutput.model_validate(data).model_dump()["metrics"] == metrics
    data["metrics"] = invalid
    with pytest.raises(ValueError, match="metrics"):
        DiagnosticOutput.model_validate(data)


@pytest.mark.parametrize("unit_length", [75, 76])
def test_derived_gradient_units_obey_the_same_limit(
    tmp_path: Path, unit_length: int,
) -> None:
    root = make_sensitivity_project(tmp_path)
    unit = "u" * unit_length
    for filename, mean in [("series.json", 3.0), ("run_b.json", 5.0), ("run_c.json", 7.0)]:
        write_json(root / filename, series([mean] * 4, unit=unit))
    if unit_length == 75:
        result = invoke(root, "parameter_sensitivity", sensitivity_inputs())
        assert result["metrics"]["gradient"]["unit"] == f"{unit}/gain"
    else:
        with pytest.raises(ValueError, match="unit"):
            invoke(root, "parameter_sensitivity", sensitivity_inputs())
