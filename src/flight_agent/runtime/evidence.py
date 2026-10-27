import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import TypeAdapter

from .agent_contracts import InvocationRun, InvocationSpec
from .contracts import AnalysisRun, AnalysisSpec, EvidenceFile, RunId

MAX_EVIDENCE_BYTES = 100 * 1024 * 1024
MAX_RECEIPT_BYTES = 8_000_000
_RUN_ID = TypeAdapter(RunId)
EvidenceLayer = Literal["numerical", "evaluation"]
Receipt = TypeVar("Receipt", AnalysisRun, InvocationRun)


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
    descriptor = EvidenceFile.model_validate(
        artifact.model_dump(include=set(EvidenceFile.model_fields))
    )
    if describe_evidence(root, descriptor.path) != descriptor:
        raise ValueError(f"Evidence integrity mismatch: {artifact.path}")
    return _path(root, artifact.path)


def receipt_name(run_id: str, *, layer: EvidenceLayer = "numerical") -> str:
    if layer not in ("numerical", "evaluation"):
        raise ValueError("Unknown evidence layer")
    return f"{layer}/analysis-runs/{_RUN_ID.validate_python(run_id)}.json"


def load_run(
    root: Path, run_id: str, spec: AnalysisSpec, *, layer: EvidenceLayer = "numerical"
) -> AnalysisRun | None:
    spec = AnalysisSpec.model_validate(spec.model_dump())
    return _load_receipt(
        root, receipt_name(run_id, layer=layer), run_id, spec.fingerprint, AnalysisRun
    )


def agent_receipt_name(run_id: str) -> str:
    return f"agent/analysis-runs/{_RUN_ID.validate_python(run_id)}.json"


def load_agent_run(root: Path, run_id: str, spec: InvocationSpec) -> InvocationRun | None:
    spec = InvocationSpec.model_validate(spec)
    return _load_receipt(root, agent_receipt_name(run_id), run_id, spec.fingerprint, InvocationRun)


def _verify_run_evidence(root: Path, run: AnalysisRun | InvocationRun) -> None:
    artifacts: list[EvidenceFile] = list(run.artifacts)
    if isinstance(run, InvocationRun):
        artifacts.extend(run.output.artifacts)
    for artifact in artifacts:
        verify_evidence(root, artifact)


def _load_receipt(
    root: Path, name: str, run_id: str, spec_fingerprint: str, model: type[Receipt]
) -> Receipt | None:
    path = _path(root, name)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError("Missing or oversized analysis receipt")
    with path.open("rb") as stream:
        content = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError("Analysis receipt exceeds its size limit")
    run = model.model_validate_json(content)
    if run.id != run_id:
        raise ValueError("Analysis receipt identity mismatch")
    if run.spec.fingerprint != spec_fingerprint:
        raise ValueError("Analysis receipt does not match the requested spec")
    _verify_run_evidence(root, run)
    return run


def persist_run(root: Path, run: AnalysisRun, *, layer: EvidenceLayer = "numerical") -> Path:
    run = AnalysisRun.model_validate(run.model_dump())
    return _persist_receipt(
        root,
        receipt_name(run.id, layer=layer),
        run,
        lambda: load_run(root, run.id, run.spec, layer=layer),
    )


def persist_agent_run(root: Path, run: InvocationRun) -> Path:
    run = InvocationRun.model_validate(run)
    return _persist_receipt(
        root,
        agent_receipt_name(run.id),
        run,
        lambda: load_agent_run(root, run.id, run.spec),
    )


def _persist_receipt(
    root: Path, name: str, run: Receipt, load_existing: Callable[[], Receipt | None]
) -> Path:
    path = _path(root, name)
    _verify_run_evidence(root, run)
    existing = load_existing()
    if existing is not None:
        if existing != run:
            raise ValueError("Analysis receipts are immutable; this run has different evidence")
        return path
    content = run.model_dump_json(indent=2).encode("utf-8")
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError("Analysis receipt exceeds its size limit")
    return _publish_bytes(path, content, lambda: load_existing() == run)


def publish_evidence(root: Path, name: str, content: bytes) -> Path:
    if len(content) > MAX_EVIDENCE_BYTES:
        raise ValueError("Evidence exceeds its size limit")
    path = _path(root, name)

    def identical() -> bool:
        actual = describe_evidence(root, name)
        return actual.sha256 == hashlib.sha256(content).hexdigest() and actual.size == len(content)

    return _publish_bytes(path, content, identical)


def _publish_bytes(path: Path, content: bytes, identical: Callable[[], bool]) -> Path:
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
            if not identical():
                raise ValueError("An immutable receipt already exists with different evidence")
    finally:
        if temporary is not None:
            temporary.unlink()
    return path
