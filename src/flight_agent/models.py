import json
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Nonempty = Annotated[str, Field(min_length=1)]
MAX_RESULT = 4_000_000
JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def finite_json(value: JsonValue) -> JsonValue:
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (ValueError, RecursionError) as exc:
        raise ValueError("JSON must contain finite numbers and bounded nesting") from exc
    return value
