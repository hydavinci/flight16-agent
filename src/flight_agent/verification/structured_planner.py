import json
import math
import re
from collections.abc import Sequence
from copy import copy
from dataclasses import replace
from typing import Self, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langsmith import tracing_context
from pydantic import BaseModel

from ..config import ModelSettings, create_model
from ..copilot import structured_response
from .contracts import Conversation
from .intake import redact_paths

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def redact_text(text: str, local_paths: Sequence[str | None] = ()) -> str:
    for path in sorted((item for item in local_paths if item), key=len, reverse=True):
        text = text.replace(path, "[local path]")
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)\S+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
        r"\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    return redact_paths(text)


def conversation_paths(conversation: Conversation) -> list[str | None]:
    tasks = [
        *conversation.archived_tasks,
        *([conversation.task] if conversation.task is not None else []),
    ]
    return [
        conversation.project_root,
        conversation.output_root,
        *(path for task in tasks for path in (task.output_root, task.result_directory)),
    ]


class StructuredPlanner:
    def __init__(self, model: BaseChatModel | None = None):
        self.model = model
        self._timeout_seconds: float | None = None

    def with_timeout(self, seconds: float) -> Self:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Planner timeout must be finite and positive")
        bounded = copy(self)
        bounded._timeout_seconds = min(seconds, self._timeout_seconds or seconds)
        return bounded

    def call(
        self,
        name: str,
        schema: type[ResponseModel],
        context: dict[str, object],
        *,
        system_prompt: str,
    ) -> ResponseModel:
        if self.model is None:
            settings = ModelSettings.from_environment()
            if self._timeout_seconds is not None:
                settings = replace(
                    settings, timeout_seconds=min(settings.timeout_seconds, self._timeout_seconds)
                )
            if settings.provider == "copilot-cli":
                return structured_response(name, schema, context, system_prompt, settings)
        model = self.model or create_model()
        tool = {
            "type": "function",
            "function": {
                "name": name,
                "description": "Return the requested bounded verification response.",
                "parameters": schema.model_json_schema(),
            },
        }
        options = {"timeout": self._timeout_seconds} if self._timeout_seconds is not None else {}
        with tracing_context(enabled=False):
            response = model.bind_tools([tool], tool_choice=name).bind(**options).invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=json.dumps(context, ensure_ascii=False, allow_nan=False)),
                ],
            )
        if (
            not isinstance(response, AIMessage)
            or response.invalid_tool_calls
            or len(response.tool_calls) != 1
            or response.tool_calls[0]["name"] != name
            or response.response_metadata.get("finish_reason") not in (None, "stop", "tool_calls")
        ):
            raise ValueError("Model must return one complete, registered structured tool call")
        return schema.model_validate(response.tool_calls[0]["args"])
