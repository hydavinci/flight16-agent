import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def test_distributions_ship_native_modules_routes_and_matlab_bridges(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
    )
    required = {
        path.relative_to(ROOT / "src").as_posix()
        for pattern in (
            "static/*.js",
            "static/*.html",
            "static/*.css",
            "static/*.svg",
            "matlab_support/*.m",
            "web_routes/*.py",
        )
        for path in (ROOT / "src/flight_agent").glob(pattern)
    }
    required.add("flight_agent/application.py")
    assert "flight_agent/static/bootstrap.js" in required
    wheel = next(tmp_path.glob("*.whl"))
    with ZipFile(wheel) as archive:
        assert required <= set(archive.namelist())
    sdist = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(sdist) as archive:
        names = {
            name.split("/", 1)[1]
            for name in archive.getnames()
            if "/" in name
        }
        assert {"src/" + name for name in required} <= names
