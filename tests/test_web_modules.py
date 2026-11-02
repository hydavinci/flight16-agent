from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.agent_helpers import make_agent_web

PREFIX = "flight_agent.web_routes."
EXPECTED = {
    ("GET", "/"): PREFIX + "pages",
    ("GET", "/workbench"): PREFIX + "pages",
    ("GET", "/static/{name}"): PREFIX + "pages",
    ("GET", "/api/workbench/bootstrap"): PREFIX + "workbench",
    ("GET", "/api/workbench/examples/{name}"): PREFIX + "workbench",
    ("GET", "/api/workbench/runs"): PREFIX + "workbench",
    ("POST", "/api/workbench/runs"): PREFIX + "workbench",
    ("GET", "/api/workbench/runs/{run_id}"): PREFIX + "workbench",
    (
        "GET",
        "/api/workbench/runs/{run_id}/artifacts/{name:path}",
    ): PREFIX + "workbench",
    ("GET", "/api/bootstrap"): PREFIX + "configuration",
    ("GET", "/api/directories"): PREFIX + "configuration",
    ("POST", "/api/directories/select"): PREFIX + "configuration",
    ("GET", "/api/resource-locations"): PREFIX + "metadata",
    ("GET", "/api/conversations/capabilities"): PREFIX + "metadata",
    ("GET", "/api/conversations"): "flight_agent.verification.routes",
    ("POST", "/api/conversations"): "flight_agent.verification.routes",
    (
        "GET",
        "/api/conversations/{conversation_id}",
    ): "flight_agent.verification.routes",
    (
        "GET",
        "/api/conversations/{conversation_id}/view",
    ): "flight_agent.verification.routes",
    (
        "POST",
        "/api/conversations/{conversation_id}/messages",
    ): "flight_agent.verification.routes",
    (
        "POST",
        "/api/conversations/{conversation_id}/confirm",
    ): "flight_agent.verification.routes",
    (
        "POST",
        "/api/conversations/{conversation_id}/resume",
    ): "flight_agent.verification.routes",
    (
        "POST",
        "/api/conversations/{conversation_id}/retry",
    ): "flight_agent.verification.routes",
    (
        "GET",
        "/api/conversations/{conversation_id}/tasks/{task_id}/artifacts/{name:path}",
    ): PREFIX + "artifacts",
}


def api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        router = getattr(route, "original_router", None)
        if router is not None:
            yield from api_routes(router.routes)


def test_exact_route_manifest_and_ownership(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    actual = {
        (method, route.path): route.endpoint.__module__
        for route in api_routes(app.routes)
        for method in route.methods
    }

    assert actual == EXPECTED


def test_extracted_apis_keep_auth_headers_and_catalog_precedence(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        headers = {"x-flight-token": bootstrap.json()["token"]}
        for path in (
            "/api/conversations",
            "/api/conversations/capabilities",
            "/api/resource-locations",
            "/api/directories?target=output",
            "/api/conversations/"
            + "a" * 32
            + "/tasks/"
            + "b" * 32
            + "/artifacts/x",
        ):
            denied = client.get(path)
            assert denied.status_code == 403
            assert denied.headers["x-content-type-options"] == "nosniff"
            assert denied.headers["cache-control"] == "no-store"
        catalog = client.get("/api/conversations/capabilities", headers=headers)
        assert catalog.status_code == 200
        assert isinstance(catalog.json(), list)
        for path in ("/", "/api/bootstrap", "/static/agent.js", "/static/missing.js"):
            result = client.get(path)
            assert "script-src 'self'" in result.headers["content-security-policy"]
            assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
            assert result.headers["cache-control"] == "no-store"
        assert client.get("/static/agent.html").status_code == 404
        assert client.get("/static/%2e%2e%2fweb.py").status_code == 404
        assert (
            client.get(
                "/api/conversations",
                headers={**headers, "Origin": "https://attacker.example"},
            ).status_code
            == 403
        )
