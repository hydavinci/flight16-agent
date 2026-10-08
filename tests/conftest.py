import json
from pathlib import Path

import pytest


@pytest.fixture
def parameter_data():
    return {
        "schema_version": "1.0",
        "aircraft_type": "demo_fixed_wing",
        "condition": "demo_cruise",
        "mass_kg": 10.0,
        "wing_area_m2": 0.8,
        "airspeed_mps": 20.0,
        "air_density_kg_m3": 1.225,
        "thrust_n": 30.0,
    }


@pytest.fixture
def result_data():
    return {
        "schema_version": "1.0",
        "aircraft_type": "demo_fixed_wing",
        "condition": "demo_cruise",
        "model_version": "demo-static-v1",
        "matlab_version": "test-engine",
        "valid": True,
        "diagnostics": [],
        "metrics": {
            "wing_loading": {"value": 122.583125, "unit": "N/m^2"},
            "dynamic_pressure": {"value": 245.0, "unit": "Pa"},
            "required_lift_coefficient": {"value": 0.5003392857142857, "unit": "1"},
            "thrust_to_weight": {"value": 0.3059148638933785, "unit": "1"},
        },
    }


@pytest.fixture
def rule_data():
    return {
        "version": "demo-rules-v1",
        "model_version": "demo-static-v1",
        "demo_only": True,
        "rules": [
            {
                "id": "DEMO-CL",
                "metric": "required_lift_coefficient",
                "operator": "<=",
                "threshold": 1.2,
                "unit": "1",
                "aircraft_type": "demo_fixed_wing",
                "condition": "demo_cruise",
                "source_id": "demo.md",
                "source_section": "演示判据",
            }
        ],
    }


@pytest.fixture
def project(tmp_path, rule_data):
    for name in ("matlab", "rules", "knowledge"):
        (tmp_path / name).mkdir()
    (tmp_path / "matlab" / "analyze_demo.m").write_text("% test entry", encoding="utf-8")
    (tmp_path / "rules" / "demo.json").write_text(
        json.dumps(rule_data, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "knowledge" / "demo.md").write_text(
        "# 演示判据\n\n升力系数 required_lift_coefficient 不超过 1.2。仅用于软件演示。",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def repository_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_engine(monkeypatch, repository_root):
    directory = repository_root / "tests" / "fixtures" / "fake_matlab"
    monkeypatch.setenv("PYTHONPATH", str(directory))
    monkeypatch.delenv("TEST_ENGINE_MODE", raising=False)
