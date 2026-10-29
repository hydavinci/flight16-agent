import json

import pytest

from flight_agent.json_io import write_json


def test_write_json_atomically_replaces_complete_document(tmp_path):
    target = tmp_path / "state.json"
    write_json(target, {"version": 1})
    write_json(target, {"version": 2, "ready": True})
    assert json.loads(target.read_text("utf-8")) == {"version": 2, "ready": True}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_json_rejects_nan(tmp_path):
    with pytest.raises(ValueError):
        write_json(tmp_path / "state.json", {"value": float("nan")})
