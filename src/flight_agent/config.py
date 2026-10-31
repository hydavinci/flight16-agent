import math
import os
import shutil
from dataclasses import dataclass
from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI
from pydantic import SecretStr


@dataclass(frozen=True)
class ModelSettings:
    model: str
    base_url: str
    api_key: SecretStr
    timeout_seconds: float
    provider: str = "openai-compatible"

    @classmethod
    def from_environment(cls) -> "ModelSettings":
        provider = os.environ.get("LLM_PROVIDER", "openai-compatible").strip()
        if provider not in ("openai-compatible", "copilot-cli"):
            raise ValueError("LLM_PROVIDER must be openai-compatible or copilot-cli")
        timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be finite and greater than zero")
        if provider == "copilot-cli":
            if os.name != "posix":
                raise ValueError("The Copilot CLI backend currently requires macOS or Linux")
            if shutil.which("copilot") is None:
                raise ValueError("Install GitHub Copilot CLI and sign in with copilot login")
            return cls(
                model=os.environ.get("LLM_MODEL", "").strip(),
                base_url="",
                api_key=SecretStr(""),
                timeout_seconds=timeout,
                provider=provider,
            )
        values = {}
        for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Set {name} in the project .env or environment")
            values[name] = value
        endpoint = urlsplit(values["LLM_BASE_URL"])
        if (
            endpoint.scheme not in ("http", "https")
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError(
                "LLM_BASE_URL must be an HTTP(S) URL without credentials/query/fragment"
            )
        return cls(
            model=values["LLM_MODEL"],
            base_url=values["LLM_BASE_URL"],
            api_key=SecretStr(values["LLM_API_KEY"]),
            timeout_seconds=timeout,
        )


def create_model() -> ChatOpenAI:
    settings = ModelSettings.from_environment()
    if settings.provider == "copilot-cli":
        raise ValueError(
            "Copilot CLI requires StructuredPlanner, not an API chat model"
        )
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
        max_retries=0,
        use_responses_api=False,
    )


_SCHEDULER_FIELDS = {
    "FLIGHT_PLANNING_WORKERS": ("planning_workers", 4, 1, 32),
    "FLIGHT_PLANNING_QUEUE": ("planning_queue", 32, 0, 256),
    "FLIGHT_LOCAL_MATLAB_SLOTS": ("local_matlab_slots", 1, 1, 8),
    "FLIGHT_REMOTE_WORKER_SLOTS": ("remote_worker_slots", 1, 1, 16),
    "FLIGHT_READ_ONLY_SLOTS": ("read_only_slots", 4, 1, 32),
    "FLIGHT_EXECUTION_TURNS": ("execution_turns", 32, 1, 64),
}


@dataclass(frozen=True)
class SchedulerSettings:
    planning_workers: int = 4
    planning_queue: int = 32
    local_matlab_slots: int = 1
    remote_worker_slots: int = 1
    read_only_slots: int = 4
    execution_turns: int = 32

    def __post_init__(self) -> None:
        for variable, (field, default, lower, upper) in _SCHEDULER_FIELDS.items():
            value = getattr(self, field)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(
                    f"{variable} must be an integer between {lower} and {upper}"
                )
        if self.execution_turns < (
            self.local_matlab_slots
            + self.remote_worker_slots
            + self.read_only_slots
        ):
            raise ValueError(
                "FLIGHT_EXECUTION_TURNS must cover the sum of execution lane slots"
            )

    @classmethod
    def from_environment(cls) -> "SchedulerSettings":
        values: dict[str, int] = {}
        for variable, (field, default, lower, upper) in _SCHEDULER_FIELDS.items():
            text = os.environ.get(variable, str(default)).strip()
            if not text or not text.isascii() or not text.isdecimal():
                raise ValueError(
                    f"{variable} must be an integer between {lower} and {upper}"
                )
            value = int(text)
            if not lower <= value <= upper:
                raise ValueError(
                    f"{variable} must be an integer between {lower} and {upper}"
                )
            values[field] = value
        return cls(**values)
