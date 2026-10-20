import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_ABSOLUTE = re.compile(r"^(?:/|~/|[A-Za-z]:[\\/]|\\\\)")
_PATH = re.compile(
    r"""(?P<quoted>"[^"\r\n]*"|'[^'\r\n]*'|`[^`\r\n]*`|“[^”\r\n]*”|‘[^’\r\n]*’)"""
    r"""|(?P<bare>(?<![A-Za-z0-9_/\\])(?:[A-Za-z]:[\\/]|\\\\|~/|/)"""
    r"""[^\s"'`“”‘’<>，。；,;]+)"""
)
_LABEL_NAME = (
    r"工程(?:目录|路径)?|项目(?:目录|路径)?|"
    r"project(?:[_ -]?(?:root|directory|dir|path))?|"
    r"输出(?:目录|路径)?|结果(?:目录|路径)?|报告(?:目录|路径)?|"
    r"output(?:[_ -]?(?:root|directory|dir|path))?"
)
_LABEL = re.compile(
    rf"(?P<label>{_LABEL_NAME})\s*(?:(?:设为|改为|为|是|在|到|[:：=]|is|to)\s*)?$",
    re.IGNORECASE,
)
_LABELED_VALUE = re.compile(
    r"(?:工程(?:目录|路径)|项目(?:目录|路径)|输出(?:目录|路径)|结果(?:目录|路径)|"
    r"报告(?:目录|路径)|(?:project|output)[_ -]?(?:root|directory|dir|path)|"
    r"(?:project|output)(?=\s*[:：=]))\s*(?:[:：=]|为|是|\bis\b|\bto\b)\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DirectoryIntake:
    project_root: str | None
    output_root: str | None
    redacted_message: str
    question: str | None = None


def _value(match: re.Match[str]) -> str:
    if match.group("quoted") is not None:
        return match.group()[1:-1].strip()
    return match.group().rstrip(".!?)]}")


def redact_paths(text: str) -> str:
    def redact(match: re.Match[str]) -> str:
        if _ABSOLUTE.match(_value(match)):
            return "[local path]"
        quoted = match.group()
        return quoted[0] + redact_paths(quoted[1:-1]) + quoted[-1]

    return _PATH.sub(redact, text)


def _path_key(value: str) -> str:
    if PureWindowsPath(value).is_absolute():
        return str(PureWindowsPath(value)).casefold()
    return str(Path(value)) if value else ""


def parse_directories(
    message: str,
    project_root: str | None = None,
    output_root: str | None = None,
    *,
    current_project: str | None = None,
    current_output: str | None = None,
) -> DirectoryIntake:
    redacted = redact_paths(message)
    projects: list[str] = []
    outputs: list[str] = []
    unlabelled: list[str] = []

    def clarify(question: str) -> DirectoryIntake:
        return DirectoryIntake(current_project, current_output, redacted, question)

    for label in _LABELED_VALUE.finditer(message):
        path = _PATH.match(message, label.end())
        if path is None or not _ABSOLUTE.match(_value(path)):
            return clarify("请用绝对路径明确工程目录或输出目录；路径含空格时请加引号。")
    for match in _PATH.finditer(message):
        value = _value(match)
        if not _ABSOLUTE.match(value):
            continue
        label_match = _LABEL.search(message[:match.start()])
        if label_match is None:
            unlabelled.append(value)
        elif re.match(r"output|输出|结果|报告", label_match["label"], re.IGNORECASE):
            outputs.append(value)
        else:
            projects.append(value)
    if unlabelled:
        if projects or outputs or len({_path_key(item) for item in unlabelled}) != 1:
            return clarify("检测到多个或未标明用途的路径，请分别标注“工程目录”和“输出目录”。")
        projects.extend(unlabelled)
    if project_root and project_root.strip():
        projects.append(project_root.strip())
    if output_root is not None:
        outputs.append(output_root.strip())
    if len({_path_key(item) for item in projects}) > 1:
        return clarify("消息与目录栏中有不同的工程路径，请明确本次使用哪个工程目录。")
    if len({_path_key(item) for item in outputs}) > 1:
        return clarify("消息与目录栏中有不同的输出路径，请明确本次使用哪个输出目录。")
    return DirectoryIntake(
        projects[0] if projects else current_project,
        (outputs[0] or None) if outputs else current_output,
        redacted,
    )


def safe_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or any(ord(char) < 32 for char in str(path)):
        raise ValueError(
            "An absolute directory without traversal or control characters is required"
        )
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Unsafe directory: symlink traversal is not allowed")
        if component.exists() and not component.is_dir():
            raise ValueError("Output directory traverses an existing file")


def validate_output_root(value: str | None, project_root: str | None = None) -> str | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    safe_directory(path)
    path = path.resolve()
    if project_root is not None and Path(project_root).is_absolute():
        project = Path(project_root).resolve()
        if path == project or path in project.parents or project in path.parents:
            raise ValueError("Output directory must not overlap the source project")
    return str(path)
