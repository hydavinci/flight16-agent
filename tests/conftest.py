import shutil
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path):
    return tmp_path


@pytest.fixture
def repository_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_engine(monkeypatch, repository_root, tmp_path):
    source = repository_root / "tests" / "fixtures" / "fake_matlab"
    directory = tmp_path / "fake_matlab"
    shutil.copytree(source, directory)
    monkeypatch.setenv("PYTHONPATH", str(directory))
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.delenv("TEST_ENGINE_MODE", raising=False)
