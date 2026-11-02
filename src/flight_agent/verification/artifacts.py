import json
import warnings
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from PIL import Image

from ..models import finite_json
from .contracts import Artifact, relative_path
from .projects import file_digest

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MEDIA = {
    ".png": ("image/png", "figure"),
    ".mat": ("application/octet-stream", "data"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "report",
    ),
    ".json": ("application/json", "record"),
    ".log": ("text/plain", "record"),
}


def validate_png(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("PNG file missing, unsafe or exceeds the 2 MB limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as image:
                if image.format != "PNG" or image.width * image.height > 16_000_000:
                    raise ValueError("Invalid PNG or image dimensions exceed limit")
                image.verify()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError(f"Invalid PNG image: {exc}") from exc


def artifact_path(directory: Path, name: str) -> Path:
    relative_path(name)
    root = directory.resolve()
    path = root / name
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"Artifact symlink is not allowed: {name}")
    if not path.is_file() or root not in path.resolve().parents:
        raise ValueError(f"Missing or unsafe artifact: {name}")
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"Artifact exceeds 100 MB: {name}")
    return path


def describe_artifact(directory: Path, name: str) -> Artifact:
    path = artifact_path(directory, name)
    if path.suffix not in MEDIA:
        raise ValueError(f"Unsupported artifact type: {name}")
    media_type, role = MEDIA[path.suffix]
    if path.suffix == ".png":
        validate_png(path)
    elif path.suffix == ".mat":
        with path.open("rb") as stream:
            if not stream.read(128).startswith(b"MATLAB "):
                raise ValueError(f"Invalid MATLAB MAT artifact: {name}")
    elif path.suffix == ".docx":
        try:
            with ZipFile(path) as document:
                if not {"word/document.xml", "[Content_Types].xml"}.issubset(document.namelist()):
                    raise ValueError(f"Invalid Word artifact: {name}")
        except BadZipFile as exc:
            raise ValueError(f"Invalid Word artifact: {name}") from exc
    elif path.suffix == ".json":
        if path.stat().st_size > 8_000_000:
            raise ValueError(f"JSON artifact exceeds 8 MB: {name}")
        finite_json(json.loads(path.read_text("utf-8")))
    return Artifact.model_validate(
        {
            "path": name,
            "sha256": file_digest(path),
            "size": path.stat().st_size,
            "media_type": media_type,
            "role": role,
        }
    )


def verify_artifact(directory: Path, artifact: Artifact) -> Path:
    actual = describe_artifact(directory, artifact.path)
    if actual != artifact:
        raise ValueError(f"Artifact integrity mismatch: {artifact.path}")
    return artifact_path(directory, artifact.path)
