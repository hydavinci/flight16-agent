import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flight_agent.cli import main


def test_openai_compatible_http_to_real_agent_to_engine_bridge(
    project, parameter_data, fake_engine, monkeypatch
):
    calls = [
        {
            "id": "run",
            "type": "function",
            "function": {"name": "run_matlab_analysis", "arguments": "{}"},
        },
        {
            "id": "check",
            "type": "function",
            "function": {"name": "check_requirements", "arguments": "{}"},
        },
        {
            "id": "search",
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "arguments": json.dumps({"query": "演示判据"}),
            },
        },
    ]
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(request)
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            index = len(received) - 1
            message = {"role": "assistant", "content": None}
            if index < len(calls):
                message["tool_calls"] = [calls[index]]
            else:
                message["content"] = "仅软件演示。参考 [KB:demo.md]。"
            response = {
                "id": f"chatcmpl-test-{index}",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if index < len(calls) else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-only")
    (project / "input.json").write_text(json.dumps(parameter_data))
    try:
        assert main(["analyze", "input.json", "--project-root", str(project)]) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert len(received) == 4
    assert received[0]["model"] == "test-model"
    assert {item["function"]["name"] for item in received[0]["tools"]} == {
        "run_matlab_analysis",
        "check_requirements",
        "search_knowledge",
    }
    last_messages = received[-1]["messages"]
    tool_results = [
        json.loads(message["content"]) for message in last_messages if message["role"] == "tool"
    ]
    assert tool_results[0]["matlab_version"] == "test-engine"
    assert tool_results[1]["status"] == "pass"
    assert tool_results[2][0]["source_id"] == "demo.md"
    report = next((project / "runs").glob("*/report.md"))
    assert "判定状态：`pass`" in report.read_text()
