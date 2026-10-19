import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from .contracts import AnalysisRun, AnalysisSpec, EvidenceFile, RunId

MAX_EVIDENCE_BYTES = 100 * 1024 * 1024
MAX_RECEIPT_BYTES = 8_000_000
_RUN_ID = TypeAdapter(RunId)
EvidenceLayer = Literal["numerical", "evaluation"]


def _path(root: Path, name: str) -> Path:
    EvidenceFile.contained_path(name)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Evidence root requires an absolute directory without traversal")
    path = root / name
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Evidence symlink traversal is unsafe")
    return path


def describe_evidence(root: Path, name: str) -> EvidenceFile:
    path = _path(root, name)
    if not path.is_file():
        raise ValueError(f"Evidence is missing: {name}")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError(f"Evidence exceeds its size limit: {name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > MAX_EVIDENCE_BYTES:
                raise ValueError(f"Evidence exceeds its size limit: {name}")
            digest.update(block)
    return EvidenceFile(path=name, sha256=digest.hexdigest(), size=size)


def verify_evidence(root: Path, artifact: EvidenceFile) -> Path:
    if describe_evidence(root, artifact.path) != artifact:
        raise ValueError(f"Evidence integrity mismatch: {artifact.path}")
    return _path(root, artifact.path)


def receipt_name(run_id: str, *, layer: EvidenceLayer = "numerical") -> str:
    if layer not in ("numerical", "evaluation"):
        raise ValueError("Unknown evidence layer")
    return f"{layer}/analysis-runs/{_RUN_ID.validate_python(run_id)}.json"


def load_run(
    root: Path, run_id: str, spec: AnalysisSpec, *, layer: EvidenceLayer = "numerical"
) -> AnalysisRun | None:
    path = _path(root, receipt_name(run_id, layer=layer))
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError("Missing or oversized analysis receipt")
    run = AnalysisRun.model_validate_json(path.read_text("utf-8"))
    if run.id != run_id:
        raise ValueError("Analysis receipt identity mismatch")
    if run.spec.fingerprint != spec.fingerprint:
        raise ValueError("Analysis receipt does not match the requested spec")
    for artifact in run.artifacts:
        verify_evidence(root, artifact)
    return run


def persist_run(
    root: Path, run: AnalysisRun, *, layer: EvidenceLayer = "numerical"
) -> Path:
    run = AnalysisRun.model_validate(run.model_dump())
    path = _path(root, receipt_name(run.id, layer=layer))
    for artifact in run.artifacts:
        verify_evidence(root, artifact)
    existing = load_run(root, run.id, run.spec, layer=layer)
    if existing is not None:
        if existing != run:
            raise ValueError("Analysis receipts are immutable; this run has different evidence")
        return path
    content = run.model_dump_json(indent=2).encode("utf-8")
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError("Analysis receipt exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".receipt-", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if load_run(root, run.id, run.spec, layer=layer) != run:
                raise ValueError("An immutable receipt already exists with different evidence")
    finally:
        if temporary is not None:
            temporary.unlink()
    return path
