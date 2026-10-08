import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from flight_agent.models import AnalysisResult


class SampleRunner:
    log = "test runner"

    def __init__(self, result_data):
        self.result_data = result_data
        self.calls = 0

    def run(self, parameters):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("Duplicate analysis is a bug")
        return AnalysisResult.model_validate(self.result_data)


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def tool_message(name, args=None, call_id=None):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id or name, "type": "tool_call"}],
    )


def successful_responses():
    return [
        tool_message("run_matlab_analysis"),
        tool_message("check_requirements"),
        tool_message("search_knowledge", {"query": "演示判据 升力系数"}),
        AIMessage(content="仅软件演示，程序判定通过。依据：[KB:demo.md]。"),
    ]


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
