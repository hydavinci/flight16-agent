import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pydantic import JsonValue

from ..runtime.contracts import fingerprint
from ..upload_models import MAX_RESULT
from .contracts import relative_path
from .intake import safe_directory
from .matlab_contracts import MatlabEntry, MatlabProjectDescriptor
from .projects import file_digest, inventory_project_files

MAX_METADATA_BYTES = 1_000_000
_MODEL_PARAMETERS = {
    "StartTime",
    "StopTime",
    "Solver",
    "SolverType",
    "FixedStep",
    "SignalLogging",
    "SignalLoggingName",
    "SaveOutput",
    "OutputSaveName",
    "SaveTime",
    "TimeSaveName",
    "ReturnWorkspaceOutputs",
    "InitFcn",
    "PreLoadFcn",
    "PostLoadFcn",
}


def _matlab_statements(text: str) -> Iterator[tuple[int, str]]:
    block_comment = False
    pending = ""
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() == "%{":
            block_comment = True
        if block_comment:
            if line.strip() == "%}":
                block_comment = False
            continue
        # Strip comments only outside quoted literals; never interpret MATLAB code.
        code = re.match(r"""(?:'(?:(?:'')|[^'])*'|"(?:(?:"")|[^"])*"|[^%'"])*""", line)
        statement = code.group().strip() if code else ""
        if not statement:
            continue
        if not pending:
            start = number
        pending += statement[:-3].rstrip() + " " if statement.endswith("...") else statement
        if len(pending) > 4096:
            yield start, pending
            return
        if not statement.endswith("..."):
            yield start, pending
            pending = ""
    if pending:
        yield start, pending  # Retain the kind, but not an incomplete function signature.


def _literal(value: str) -> JsonValue:
    if re.fullmatch(r"'(?:''|[^'])*'", value):
        return value[1:-1].replace("''", "'")
    if re.fullmatch(r'"(?:""|[^"])*"', value):
        return value[1:-1].replace('""', '"')
    result: JsonValue = json.loads(value)
    # Reject non-MATLAB JSON objects/null and JSON's nonfinite extensions.
    pending = [result]
    while pending:
        item = pending.pop()
        if isinstance(item, dict) or item is None:
            raise ValueError("Not a simple MATLAB literal")
        if isinstance(item, list):
            pending.extend(item)
    fingerprint(result)
    return result


def _xml_metadata(text: str, *, model: bool) -> list[tuple[str, str]]:
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML declarations/entities are not supported")
    root = ElementTree.fromstring(text)
    values = []
    for element in root.iter():
        name = element.attrib.get("Name") if model else element.tag.rsplit("}", 1)[-1]
        if name and (not model or name in _MODEL_PARAMETERS) and element.text:
            value = element.text.strip()
            if value and not list(element):
                values.append((name, value))
    return values


def discover_matlab_project(root: str) -> MatlabProjectDescriptor:
    directory = Path(root).expanduser()
    safe_directory(directory)
    if not directory.is_dir():
        raise ValueError("Provide an existing absolute project directory on the Agent host")
    directory = directory.resolve()
    files = inventory_project_files(directory)
    models = [item.path for item in files if Path(item.path).suffix.lower() in {".slx", ".mdl"}]
    project_files = [item.path for item in files if Path(item.path).suffix.lower() == ".prj"]
    entries: list[MatlabEntry] = []
    notes = [
        "Static candidates and literal declarations are not confirmed execution selections "
        "or effective runtime settings.",
        "Only trusted project code may run; copying is not a security sandbox.",
    ]
    settings: dict[str, JsonValue] = {
        "matlab_literals": [],
        "model_parameters": [],
        "project_metadata": [],
        "documentation": [],
    }
    remaining = MAX_METADATA_BYTES
    settings_size = len(json.dumps(settings, ensure_ascii=False).encode("utf-8"))
    settings_truncated = False

    def append_setting(group: str, value: dict[str, JsonValue]) -> bool:
        nonlocal settings_size, settings_truncated
        records = settings[group]
        assert isinstance(records, list)
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + (2 if records else 0)
        if settings_size + size > MAX_RESULT:
            settings_truncated = True
            notes.append(
                f"{value['path']}: serialized metadata limit exceeded; "
                "settings truncated and remaining metadata unresolved."
            )
            return False
        records.append(value)
        settings_size += size
        return True

    def record(group: str, path: str, location: str, name: str, value: JsonValue) -> bool:
        return append_setting(
            group, {"path": path, "location": location, "name": name, "value": value}
        )

    def xml(text: str, path: str, location: str, *, model: bool) -> None:
        try:
            values = _xml_metadata(text, model=model)
        except (ElementTree.ParseError, ValueError) as exc:
            notes.append(f"{path}:{location}: unresolved XML metadata ({exc}).")
            return
        for name, value in values:
            if not record(
                "model_parameters" if model else "project_metadata",
                path,
                f"{location}:{name}",
                name,
                value,
            ):
                break

    for item in files:
        path = directory / item.path
        suffix = path.suffix.lower()
        is_readme = path.name.lower().startswith("readme")
        if suffix not in {".m", ".mdl", ".slx", ".prj"} and not is_readme:
            continue
        if settings_truncated and suffix != ".m":
            continue
        safe_directory(path.parent)
        if path.is_symlink():
            raise ValueError(f"Project changed to a symlink: {item.path}")
        if suffix == ".slx":
            try:
                with ZipFile(path) as archive:
                    for member in archive.infolist():
                        if not member.filename.lower().endswith(".xml"):
                            continue
                        try:
                            relative_path(member.filename)
                        except ValueError:
                            notes.append(f"{item.path}: unsafe archive member ignored.")
                            continue
                        if member.file_size > remaining:
                            notes.append(f"{item.path}:{member.filename}: metadata limit exceeded.")
                            continue
                        with archive.open(member) as stream:
                            content = stream.read(remaining + 1)
                        if len(content) > remaining:
                            notes.append(f"{item.path}:{member.filename}: metadata limit exceeded.")
                            continue
                        remaining -= len(content)
                        xml(content.decode("utf-8-sig"), item.path, member.filename, model=True)
                        if settings_truncated:
                            break
            except (BadZipFile, UnicodeError, RuntimeError, NotImplementedError) as exc:
                notes.append(f"{item.path}: unresolved model archive ({exc}).")
            continue
        with path.open("rb") as stream:
            content = stream.read(MAX_METADATA_BYTES + 1)
        truncated = len(content) > MAX_METADATA_BYTES
        try:
            text = content[:MAX_METADATA_BYTES].decode("utf-8-sig")
        except UnicodeError:
            notes.append(f"{item.path}: unresolved non-UTF-8 static metadata.")
            continue
        if suffix == ".m":
            statements = list(_matlab_statements(text))
            first_line, first = statements[0] if statements else (1, "")
            if re.match(r"^classdef\b", first):
                notes.append(f"{item.path}: classdef is not a script/function entry.")
            elif not first and (truncated or statements):
                notes.append(f"{item.path}: unresolved entry signature (metadata limit).")
            else:
                kind: Literal["script", "function"] = (
                    "function" if re.match(r"^function\b", first) else "script"
                )
                signature = (
                    first
                    if kind == "function" and len(first) <= 4096 and not first.endswith(" ")
                    else None
                )
                if kind == "function" and signature is None:
                    notes.append(f"{item.path}: unresolved function signature (metadata limit).")
                entries.append(
                    MatlabEntry(
                        path=item.path,
                        kind=kind,
                        signature=signature,
                        evidence=[f"{item.path}:{first_line}"],
                    )
                )
        if settings_truncated:
            continue
        if truncated or len(content) > remaining:
            notes.append(f"{item.path}: metadata limit exceeded; settings unresolved.")
            continue
        remaining -= len(content)
        if suffix == ".m":
            for line, statement in _matlab_statements(text):
                if re.match(r"^(function|classdef)\b", statement):
                    break
                assignment = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,62})\s*=\s*(.+?);?", statement)
                if assignment:
                    name, raw = assignment.groups()
                    try:
                        value = _literal(raw)
                    except (ValueError, RecursionError):
                        notes.append(f"{item.path}:{line}: unresolved assignment to {name}.")
                        continue
                    if not record("matlab_literals", item.path, f"{item.path}:{line}", name, value):
                        break
        elif suffix == ".mdl":
            for line, statement in enumerate(text.splitlines(), 1):
                parameter = re.fullmatch(r'\s*(\w+)\s+(".*?"|\S+)\s*', statement)
                if parameter and parameter[1] in _MODEL_PARAMETERS:
                    if not record(
                        "model_parameters",
                        item.path,
                        f"{item.path}:{line}",
                        parameter[1],
                        parameter[2].strip('"'),
                    ):
                        break
        elif suffix == ".prj":
            xml(text, item.path, item.path, model=False)
        elif is_readme:
            append_setting("documentation", {"path": item.path, "text": text})

    for group in ("matlab_literals", "model_parameters"):
        declarations = settings[group]
        assert isinstance(declarations, list)
        seen: dict[str, JsonValue] = {}
        for declaration in declarations:
            assert isinstance(declaration, dict) and isinstance(declaration["name"], str)
            name = declaration["name"]
            if name in seen and seen[name] != declaration["value"]:
                notes.append(f"Static setting conflict for {name}; retain all declarations.")
            seen[name] = declaration["value"]
    if inventory_project_files(directory) != files:
        raise ValueError("Project changed during static discovery; rediscover its fingerprint")
    identity = {"files": [item.model_dump(mode="json") for item in files], "settings": settings}
    return MatlabProjectDescriptor(
        root=str(directory),
        fingerprint=fingerprint(identity),
        files=files,
        models=models,
        entries=entries,
        project_files=project_files,
        notes=notes,
        settings=settings,
    )


def snapshot_matlab_project(project: MatlabProjectDescriptor, destination: Path) -> Path:
    safe_directory(Path(project.root))
    safe_directory(destination)
    source = Path(project.root).resolve()
    destination = destination.resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError(
            "Runtime project copy must be outside and not overlap the source directory"
        )
    actual = discover_matlab_project(project.root)
    if actual != project:
        raise ValueError("Project changed after baseline confirmation; confirm its new fingerprint")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for item in project.files:
            original = source / item.path
            target = destination / item.path
            safe_directory(original.parent)
            safe_directory(target.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            if original.is_symlink():
                raise ValueError(f"Project changed to a symlink: {item.path}")
            shutil.copyfile(original, target)
            if file_digest(target) != item.sha256:
                raise ValueError(f"Project changed while copying: {item.path}")
        if inventory_project_files(source) != project.files:
            raise ValueError("Project changed while its runtime copy was being prepared")
        if inventory_project_files(destination) != project.files:
            raise ValueError("Runtime project copy differs from the confirmed inventory")
    except OSError as exc:
        raise ValueError("Project changed while its runtime copy was being prepared") from exc
    return destination
