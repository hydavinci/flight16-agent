import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

TOKEN = "test-worker-token-with-at-least-32-characters"
HEADERS = {"Authorization": "Bearer " + TOKEN}


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def tool_message(name, args=None, call_id=None):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id or name, "type": "tool_call"}],
    )


def web_module():
    from flight_agent import web

    return web


def worker():
    from flight_agent import worker

    return worker


def headers(client):
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    return {"X-Flight-Token": bootstrap.json()["token"]}


def wait_job(client, job_id, headers=HEADERS, prefix="/verification/runs"):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"{prefix}/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] not in ("queued", "running"):
            return record
        time.sleep(0.02)
    pytest.fail("Task did not reach a terminal state")


@contextmanager
def serve_app(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Test HTTP server did not start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Test HTTP server did not stop")
