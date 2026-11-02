import hashlib

import pytest

from flight_agent.runtime.agent_contracts import AgentBaseline, AgentPlan
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.project_snapshots import (
    ProjectSnapshot,
    SnapshotFile,
    SnapshotManifest,
)
from tests.agent_helpers import analysis_plan_data


def test_manifest_identity_is_exact_canonical_content():
    manifest = SnapshotManifest(
        files=[
            SnapshotFile(path="a.m", sha256="a" * 64, size=1),
            SnapshotFile(path="data/b.mat", sha256="b" * 64, size=2),
        ]
    )
    expected = (
        '{"files":[{"path":"a.m","sha256":"'
        + "a" * 64
        + '","size":1},{"path":"data/b.mat","sha256":"'
        + "b" * 64
        + '","size":2}],"schema_version":"1"}'
    ).encode()
    assert manifest.canonical_bytes() == expected
    assert manifest.snapshot_id == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("path", ["../a.m", "/a.m", "a//b", "a\\b", "C:x", "a\nb"])
def test_manifest_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        SnapshotFile(path=path, sha256="a" * 64, size=1)


def test_manifest_rejects_duplicates_unsorted_paths_and_budget_overflow():
    a = SnapshotFile(path="a", sha256="a" * 64, size=1)
    b = SnapshotFile(path="b", sha256="b" * 64, size=1)
    for files in (
        [a, a],
        [b, a],
        [a] * 5001,
        [a.model_copy(update={"size": 512 * 1024 * 1024 + 1})],
    ):
        with pytest.raises(ValueError):
            SnapshotManifest(files=files)


def test_binding_checks_content_identity():
    manifest = SnapshotManifest(files=[])
    with pytest.raises(ValueError, match="identity"):
        ProjectSnapshot(
            snapshot_id="f" * 64,
            source_fingerprint="a" * 64,
            manifest=manifest,
        )


def test_empty_snapshot_map_preserves_historical_baseline_digest():
    old = {
        **AgentPlan.model_validate(analysis_plan_data()).model_dump(mode="json"),
        "version": 1,
        "confirmed": True,
        "confirmed_at": "2026-10-31T00:00:00+00:00",
        "capability_snapshots": {},
    }
    baseline = AgentBaseline.model_validate(old)
    assert "project_snapshots" not in baseline.model_dump(mode="json")
    assert fingerprint(baseline.model_dump(mode="json")) == fingerprint(old)
