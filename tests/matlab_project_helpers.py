from pathlib import Path
from zipfile import ZipFile


def prepared_project(
    root: Path, *, model_name: str = "plant", signal_name: str = "response"
) -> Path:
    root.mkdir(parents=True)
    (root / "initialize.m").write_text(
        "% Existing initialization entry; discovery must never run it.\n"
        "StopTime = 12;\n"
        "condition = 'cruise';\n"
        "error('Static fixture only');\n",
        encoding="utf-8",
    )
    with ZipFile(root / f"{model_name}.slx", "w") as archive:
        archive.writestr(
            "simulink/configSet0.xml",
            '<ConfigSet><P Name="StopTime">12</P>'
            '<P Name="SignalLoggingName">logsout</P></ConfigSet>',
        )
    (root / "README.md").write_text(
        f"Existing initializer: initialize.m\nModel: {model_name}.slx\n"
        f"Input signal: command (deg)\nResponse signal: {signal_name} (deg/s)\n"
        "Condition: cruise\nOutput: results/output.mat\nDependency: data/trim.mat\n",
        encoding="utf-8",
    )
    (root / "data").mkdir()
    (root / "data" / "trim.mat").write_bytes(b"static dependency fixture")
    return root
