from pathlib import Path

import pytest
from playwright.sync_api import expect

expect.set_options(timeout=10_000)


@pytest.fixture
def project(tmp_path):
    return tmp_path


@pytest.fixture
def repository_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_engine(monkeypatch, repository_root):
    directory = repository_root / "tests" / "fixtures" / "fake_matlab"
    monkeypatch.setenv("PYTHONPATH", str(directory))
    monkeypatch.delenv("TEST_ENGINE_MODE", raising=False)
