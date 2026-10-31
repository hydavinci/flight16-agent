import json
import mimetypes
import sqlite3
from pathlib import Path
from typing import TypeAlias

from ..runtime.evidence import agent_receipt_name, describe_evidence
from ..runtime.observations import OutputArtifact
from .contracts import Conversation

Row: TypeAlias = dict[str, str | int | None]

_TASK_STATE_FIELDS = (
    "schema_version",
    "baseline_sha256",
    "nodes",
    "decisions",
    "outputs",
    "budget_state",
    "progress_calls",
    "checkpoints",
    "checkpoint_sha256",
    "reuse_sources",
    "reuse_mappings",
    "result_directory",
    "output_root",
)
_TASK_FIELDS = frozenset(
    (
        "id",
        "baseline",
        "stage",
        "result",
        "calls",
        "pending_specs",
        "analysis_runs",
        "artifacts",
        *_TASK_STATE_FIELDS,
    )
)


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _position(row: Row) -> int:
    value = row["position"]
    if type(value) is not int:
        raise ValueError("Artifact position must be an integer")
    return value


def encode(
    record: Conversation,
    existing_artifacts: list[Row] | None = None,
) -> dict[str, list[Row]]:
    data = record.model_dump(mode="json")
    result: dict[str, list[Row]] = {
        name: [] for name in ("conversations", "messages", "tasks", "calls", "artifacts")
    }
    existing = {
        (row["task_id"], row["slot"], row["call_id"]): row
        for row in sorted(
            (existing_artifacts or []),
            key=_position,
        )
        if row["slot"] in ("receipt", "checkpoint")
    }
    next_positions = {
        (task_id, slot): 1 + max(
            (
                _position(row)
                for row in (existing_artifacts or [])
                if row["task_id"] == task_id and row["slot"] == slot
            ),
            default=-1,
        )
        for task_id in {str(row["task_id"]) for row in (existing_artifacts or [])}
        for slot in ("receipt", "checkpoint")
    }
    result["conversations"].append(
        {
            "id": record.id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "status": record.status,
            "project_root": record.project_root,
            "resource_location": record.resource_location,
            "output_root": record.output_root,
            "pending_decision_json": canonical(data["pending_decision"]),
            "error": record.error,
            "current_task_id": record.task.id if record.task else None,
            "state_json": canonical(
                {
                    key: data[key]
                    for key in ("schema_version", "output_root_reset", "turn_origin")
                }
            ),
        }
    )
    for position, message in enumerate(record.messages):
        result["messages"].append(
            {
                "conversation_id": record.id,
                "position": position,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
                "response_kind": message.response_kind,
                "gaps_json": canonical(message.model_dump(mode="json")["gaps"]),
            }
        )
    tasks = [
        *((task, position) for position, task in enumerate(record.archived_tasks)),
        *([(record.task, None)] if record.task is not None else []),
    ]
    if len({task.id for task, _ in tasks}) != len(tasks):
        raise ValueError("Current and archived task identities must be unique")
    for task, archive_position in tasks:
        result["artifacts"].extend(
            dict(row)
            for row in (existing_artifacts or [])
            if row["task_id"] == task.id
            and row["slot"] in ("receipt", "checkpoint")
        )
        value = task.model_dump(mode="json")
        if set(value) != _TASK_FIELDS:
            raise ValueError("Unsupported task state fields require an explicit codec update")
        status = (
            task.result.execution_status
            if task.result is not None
            else "interrupted"
            if any(node.status == "interrupted" for node in task.nodes)
            else "failed"
            if any(node.status == "failed" for node in task.nodes)
            else "running"
            if any(node.status == "running" for node in task.nodes)
            else "completed"
            if all(node.status == "completed" for node in task.nodes)
            else "pending"
        )
        result["tasks"].append(
            {
                "id": task.id,
                "conversation_id": record.id,
                "archive_position": archive_position,
                "baseline_version": task.baseline.version,
                "stage": task.stage,
                "status": status,
                "baseline_json": canonical(value["baseline"]),
                "state_json": canonical({key: value[key] for key in _TASK_STATE_FIELDS}),
                "result_json": canonical(value["result"]),
                "created_at": record.updated_at,
                "updated_at": record.updated_at,
            }
        )
        runs = {run.id: (position, run) for position, run in enumerate(task.analysis_runs)}
        if len(runs) != len(task.analysis_runs):
            raise ValueError("Invocation receipt identities must be unique")
        call_ids = {call.id for call in task.calls}
        if (
            len(call_ids) != len(task.calls)
            or not set(runs) <= call_ids
            or not set(task.pending_specs) <= call_ids
        ):
            raise ValueError("Invocation metadata must belong to unique recorded calls")
        for position, call in enumerate(task.calls):
            entry = runs.get(call.id)
            run = entry[1] if entry is not None else None
            spec = task.pending_specs.get(call.id) or (
                run.spec if run is not None else None
            )
            result["calls"].append(
                {
                    "id": call.id,
                    "task_id": task.id,
                    "position": position,
                    "node_id": call.node_id,
                    "capability": call.tool,
                    "capability_version": call.tool_version,
                    "baseline_version": call.baseline_version,
                    "status": call.status,
                    "inputs_json": canonical(call.inputs),
                    "spec_json": canonical(
                        spec.model_dump(mode="json") if spec else None
                    ),
                    "spec_pending": int(call.id in task.pending_specs),
                    "output_json": canonical(call.output),
                    "receipt_json": canonical(
                        run.model_dump(mode="json") if run else None
                    ),
                    "receipt_position": entry[0] if entry is not None else None,
                    "error": call.error,
                    "started_at": call.started_at,
                    "finished_at": call.finished_at,
                }
            )

        def disk_descriptor(slot: str, call_id: str, path: str) -> OutputArtifact:
            previous = existing.get((task.id, slot, call_id))
            if previous is not None and previous["relative_path"] == path:
                descriptor = OutputArtifact.model_validate_json(
                    str(previous["descriptor_json"])
                )
                if (
                    slot != "checkpoint"
                    or descriptor.sha256 == task.checkpoint_sha256.get(call_id)
                ):
                    return descriptor
            return OutputArtifact(
                **describe_evidence(Path(task.result_directory), path).model_dump()
            )

        def disk_position(
            slot: str,
            call_id: str,
            descriptor: OutputArtifact,
        ) -> int:
            previous = existing.get((task.id, slot, call_id))
            if previous is not None:
                previous_descriptor = OutputArtifact.model_validate_json(
                    str(previous["descriptor_json"])
                )
                if previous_descriptor == descriptor:
                    return _position(previous)
                if slot == "receipt":
                    raise ValueError("Invocation receipt artifact is immutable")
            key = (task.id, slot)
            position = next_positions.get(key, 0)
            next_positions[key] = position + 1
            return position

        def artifact(
            slot: str,
            position: int,
            descriptor: OutputArtifact,
            call_id: str | None = None,
        ) -> None:
            media_type = (
                mimetypes.guess_type(descriptor.path)[0]
                or "application/octet-stream"
            )
            role = (
                slot
                if slot in ("receipt", "checkpoint")
                else "report"
                if descriptor.path.endswith((".docx", ".pdf"))
                else "figure"
                if media_type.startswith("image/")
                else "data"
            )
            row: Row = {
                "task_id": task.id,
                "slot": slot,
                "position": position,
                "call_id": call_id,
                "relative_path": descriptor.path,
                "sha256": descriptor.sha256,
                "size": descriptor.size,
                "media_type": media_type,
                "role": role,
                "descriptor_json": canonical(descriptor.model_dump(mode="json")),
            }
            previous = next(
                (
                    item
                    for item in result["artifacts"]
                    if item["task_id"] == task.id
                    and item["slot"] == slot
                    and item["position"] == position
                ),
                None,
            )
            if previous is not None:
                if previous != row:
                    raise ValueError("Artifact position is already immutable")
                return
            result["artifacts"].append(row)

        for position, descriptor in enumerate(task.artifacts):
            artifact("task", position, descriptor)
        run_position = 0
        output_position = 0
        for run in task.analysis_runs:
            descriptor = disk_descriptor(
                "receipt", run.id, agent_receipt_name(run.id)
            )
            artifact(
                "receipt",
                disk_position("receipt", run.id, descriptor),
                descriptor,
                run.id,
            )
            for item in run.artifacts:
                artifact("run", run_position, item, run.id)
                run_position += 1
            for item in run.output.artifacts:
                artifact("output", output_position, item, run.id)
                output_position += 1
        for call_id, path in sorted(task.checkpoints.items()):
            descriptor = disk_descriptor("checkpoint", call_id, path)
            if descriptor.sha256 != task.checkpoint_sha256.get(call_id):
                raise ValueError("Checkpoint digest does not match the recorded state")
            artifact(
                "checkpoint",
                disk_position("checkpoint", call_id, descriptor),
                descriptor,
                call_id,
            )
    return result


def decode(connection: sqlite3.Connection, conversation_id: str) -> Conversation:
    header = connection.execute(
        "SELECT * FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if header is None:
        raise KeyError("Unknown conversation")
    data = json.loads(header["state_json"])
    data.update(
        {
            key: header[key]
            for key in (
                "id",
                "created_at",
                "updated_at",
                "status",
                "project_root",
                "resource_location",
                "output_root",
                "error",
            )
        }
    )
    data["pending_decision"] = json.loads(header["pending_decision_json"])
    data["messages"] = [
        {
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
            "response_kind": row["response_kind"],
            "gaps": json.loads(row["gaps_json"]),
        }
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY position",
            (conversation_id,),
        )
    ]
    data["task"] = None
    data["archived_tasks"] = []
    for row in connection.execute(
        """SELECT * FROM tasks WHERE conversation_id=?
           ORDER BY archive_position IS NULL, archive_position, id""",
        (conversation_id,),
    ):
        task = json.loads(row["state_json"])
        task.update(
            {
                "id": row["id"],
                "stage": row["stage"],
                "baseline": json.loads(row["baseline_json"]),
                "result": json.loads(row["result_json"]),
                "calls": [],
                "pending_specs": {},
                "analysis_runs": [],
                "artifacts": [],
            }
        )
        receipts = []
        for call in connection.execute(
            "SELECT * FROM calls WHERE task_id=? ORDER BY position", (row["id"],)
        ):
            task["calls"].append(
                {
                    "id": call["id"],
                    "node_id": call["node_id"],
                    "tool": call["capability"],
                    "tool_version": call["capability_version"],
                    "baseline_version": call["baseline_version"],
                    "status": call["status"],
                    "started_at": call["started_at"],
                    "finished_at": call["finished_at"],
                    "inputs": json.loads(call["inputs_json"]),
                    "output": json.loads(call["output_json"]),
                    "error": call["error"],
                }
            )
            if call["spec_pending"]:
                task["pending_specs"][call["id"]] = json.loads(call["spec_json"])
            if call["receipt_position"] is not None:
                receipts.append(
                    (call["receipt_position"], json.loads(call["receipt_json"]))
                )
        task["analysis_runs"] = [receipt for _, receipt in sorted(receipts)]
        task["artifacts"] = [
            json.loads(artifact["descriptor_json"])
            for artifact in connection.execute(
                """SELECT descriptor_json FROM artifacts
                   WHERE task_id=? AND slot='task' ORDER BY position""",
                (row["id"],),
            )
        ]
        if row["baseline_version"] != task["baseline"]["version"]:
            raise ValueError(
                "Stored baseline version disagrees with its validated payload"
            )
        if row["id"] == header["current_task_id"]:
            data["task"] = task
        elif row["archive_position"] is not None:
            data["archived_tasks"].append(task)
        else:
            raise ValueError("Stored task is neither current nor archived")
    record = Conversation.model_validate(data)
    if record.id != conversation_id:
        raise ValueError("Conversation identity mismatch")
    return record
