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
            "Copilot CLI supports directory workflows only; CLI analyze and workbench "
            "Agent analysis require LLM_PROVIDER=openai-compatible"
        )
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
        max_retries=0,
        use_responses_api=False,
    )
