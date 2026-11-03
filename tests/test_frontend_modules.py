import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.agent_helpers import make_agent_web

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/flight_agent/static"
MODULES = {
    "agent.js",
    "api.js",
    "state.js",
    "render.js",
    "task-evidence.js",
    "resize-preferences.js",
    "bootstrap.js",
}


def test_agent_loads_native_modules_from_explicit_allowlist(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        html = client.get("/").text
        assert re.search(
            r'<script\b(?=[^>]*\btype="module")'
            r'(?=[^>]*\bsrc="/static/agent\.js")[^>]*>',
            html,
        )
        for name in MODULES:
            response = client.get("/static/" + name)
            assert response.status_code == 200
            assert response.headers["content-type"].split(";")[0] == "text/javascript"
            assert response.headers["x-content-type-options"] == "nosniff"
        for name in (
            "agent.html",
            "unknown.js",
            "package.json",
            "uv.lock",
            "api.js.map",
        ):
            assert client.get("/static/" + name).status_code == 404


def test_import_graph_is_local_complete_and_acyclic():
    imports = {}
    for name in MODULES:
        source = (STATIC / name).read_text("utf-8")
        dependencies = re.findall(r'\bfrom\s+["\']\./([^"\']+)["\']', source)
        assert set(dependencies) <= MODULES
        assert "innerHTML" not in source
        assert "require(" not in source
        assert "window.state" not in source
        imports[name] = dependencies

    def visit(name, visiting, visited):
        assert name not in visiting, f"Circular module dependency: {name}"
        if name in visited:
            return
        for dependency in imports[name]:
            visit(dependency, visiting | {name}, visited)
        visited.add(name)

    visited = set()
    visit("agent.js", set(), visited)
    assert visited == MODULES
    assert (STATIC / "agent.js").read_text("utf-8").strip() == (
        'import {start} from "./bootstrap.js";\n\nstart();'
    )
    for name in (
        "package.json",
        "package-lock.json",
        "webpack.config.js",
        "vite.config.js",
    ):
        assert not (ROOT / name).exists()
    assert "fetch(" not in (STATIC / "state.js").read_text("utf-8")
    assert "document." not in (STATIC / "api.js").read_text("utf-8")
    assert "document." not in (STATIC / "state.js").read_text("utf-8")
