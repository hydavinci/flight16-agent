import hashlib
import json
import os
import shutil
from pathlib import Path

from .contracts import FileDigest, ProjectConfig, ProjectDescriptor, relative_path

MAX_PROJECT_FILES = 5000
MAX_PROJECT_BYTES = 512 * 1024 * 1024
IGNORED = {".git", ".DS_Store", "__pycache__", "slprj", "codegen", ".pytest_cache"}


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _f16_profile() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "name": "F-16 longitudinal pitch inner loop",
            "model": "F16_CAS_Gain/F16BLOCK_ROBUST_SURFACE_CONTROL.slx",
            "initializer": "F16_CAS_Gain/initialize_F16_ROBUST_SURFACE_workspace.m",
            "working_directory": "F16Sim",
            "dependencies": [
                "F16_CAS_Gain/data/F16TrimGrid.mat",
                "F16_CAS_Gain/data/F16GainSurface.mat",
                "F16_CAS_Gain/data/F16ControlEffectiveness.mat",
                "F16Sim",
                "F16Sim/F16_Actuator_Library.mdl",
            ],
            "trim_selector": "F16_CAS_Gain/select_F16_trim.m",
            "trim_selection_variable": "F16CasInitialConditionSelection",
            "input_signal": {"name": "stick_lb", "unit": "lb"},
            "response_signal": {"name": "q_deg_s", "unit": "deg/s"},
            "matlab_release": "R2024b",
            "native_modules": [
                {"name": "nlplant", "directory": "F16Sim", "source": "F16Sim/nlplant.c"}
            ],
        }
    )


class ProjectInspector:
    def inspect(self, root: str) -> ProjectDescriptor:
        directory = Path(root).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("Provide an existing absolute project directory on the worker")
        if directory.is_symlink():
            raise ValueError("The project directory must not be a symlink")
        directory = directory.resolve()
        manifest = directory / "flight-agent.project.json"
        profile = _f16_profile()
        if manifest.is_symlink():
            raise ValueError("Project manifest must not be a symlink")
        if manifest.is_file():
            if manifest.stat().st_size > 1_000_000:
                raise ValueError("Project manifest exceeds 1 MB")
            config = ProjectConfig.model_validate_json(manifest.read_text("utf-8"))
        elif (directory / profile.model).is_file():
            config = profile
        else:
            candidates = sorted(
                str(path.relative_to(directory))
                for pattern in ("*.slx", "*/*.slx")
                for path in directory.glob(pattern)
            )[:20]
            raise ValueError(
                "No registered project entry. Provide flight-agent.project.json; "
                "discovery will not guess model, signals or initializer. Model candidates: "
                + ", ".join(candidates)
            )
        if config.backend != "simulink":
            raise ValueError(f"Unsupported analysis backend: {config.backend}")
        files = self._files(directory)
        registered = [config.model, config.initializer, *config.dependencies]
        if config.working_directory:
            registered.append(config.working_directory)
        if config.trim_selector:
            registered.append(config.trim_selector)
        for module in config.native_modules:
            registered.append(module.directory)
            if module.source:
                registered.append(module.source)
        for entry in registered:
            if not (directory / entry).exists():
                raise ValueError(f"Missing project resource: {entry}")
        for optional_entry in (config.model, config.initializer, config.trim_selector):
            if optional_entry and not (directory / optional_entry).is_file():
                raise ValueError(f"Project entry must be a regular file: {optional_entry}")
        identity = {
            "config": config.model_dump(mode="json"),
            "files": [item.model_dump() for item in files],
        }
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        warnings = [
            "Only trusted project code may run; project copying is not a security sandbox.",
            "Independent manual reference results are required for final numerical acceptance.",
        ]
        if config.native_modules:
            warnings.append(
                "Native modules must match the worker platform. Missing binaries are built "
                "from registered trusted sources in the isolated copy when a compiler is available."
            )
        return ProjectDescriptor(
            root=str(directory),
            fingerprint=fingerprint,
            config=config,
            files=files,
            warnings=warnings,
        )

    def _files(self, root: Path) -> list[FileDigest]:
        files: list[FileDigest] = []
        total = 0
        for current, directories, names in os.walk(root, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in IGNORED)
            for name in directories:
                if (Path(current) / name).is_symlink():
                    raise ValueError(f"Project symlink is not supported: {name}")
            for name in sorted(names):
                if name in IGNORED or name.endswith((".asv", ".slxc")):
                    continue
                path = Path(current) / name
                relative = relative_path(path.relative_to(root).as_posix())
                if path.is_symlink() or not path.is_file():
                    raise ValueError(
                        f"Project symlink or special file is not supported: {relative}"
                    )
                size = path.stat().st_size
                total += size
                if len(files) >= MAX_PROJECT_FILES or total > MAX_PROJECT_BYTES:
                    raise ValueError("Project exceeds the 5000-file / 512 MB snapshot limit")
                files.append(FileDigest(path=relative, sha256=file_digest(path), size=size))
        return sorted(files, key=lambda item: item.path)


def snapshot_project(project: ProjectDescriptor, destination: Path) -> Path:
    source = Path(project.root).resolve()
    destination = destination.resolve()
    if destination == source or source in destination.parents:
        raise ValueError("Runtime project copy must be outside the source directory")
    actual = ProjectInspector().inspect(project.root)
    if actual.fingerprint != project.fingerprint:
        raise ValueError("Project changed after baseline confirmation; confirm its new fingerprint")
    destination.mkdir(parents=True, exist_ok=False)
    for item in project.files:
        original = source / item.path
        target = destination / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            raise ValueError(f"Project changed to a symlink: {item.path}")
        shutil.copyfile(original, target)
        if file_digest(target) != item.sha256:
            raise ValueError(f"Project changed while copying: {item.path}")
    if ProjectInspector().inspect(project.root).fingerprint != project.fingerprint:
        raise ValueError("Project changed while its runtime copy was being prepared")
    return destination
