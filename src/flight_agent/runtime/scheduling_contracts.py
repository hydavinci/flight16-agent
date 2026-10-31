from dataclasses import dataclass
from typing import Literal

from .contracts import Effect

ExecutionLane = Literal["local_matlab", "remote_worker", "read_only"]


@dataclass(frozen=True)
class SchedulingMetadata:
    lane: ExecutionLane


def validate_scheduling(
    effect: Effect, metadata: SchedulingMetadata | None
) -> SchedulingMetadata:
    if metadata is None and effect in ("read_only", "write_delivery"):
        metadata = SchedulingMetadata("read_only")
    if metadata is None or metadata.lane not in ("local_matlab", "remote_worker", "read_only"):
        raise ValueError("Capability requires valid scheduling metadata")
    if effect == "execute_project" and metadata.lane in ("local_matlab", "remote_worker"):
        return SchedulingMetadata(metadata.lane)
    if effect in ("read_only", "write_delivery") and metadata.lane == "read_only":
        return SchedulingMetadata("read_only")
    raise ValueError("Capability effect conflicts with scheduling metadata")
