from datetime import UTC, datetime
from typing import Literal

from .contracts import Conversation, ConversationMessage, PendingDecision

RESTART_MESSAGE = (
    "Service restarted before this request completed. No queued request was replayed; "
    "review the saved state and explicitly resend, confirm, or resume."
)
ARCHIVED_RECONCILIATION_MESSAGE = (
    "Unresolved calls remain only in archived task history. No request was replayed; "
    "explicitly reconcile the saved call IDs and evidence before continuing."
)


def has_unresolved_scheduling(record: Conversation) -> bool:
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    return any(
        call.status in ("running", "interrupted")
        for task in tasks
        for call in task.calls
    )


def conservative_pending_decision(
    record: Conversation,
    message: str,
    *,
    unresolved: bool | None = None,
) -> PendingDecision:
    unresolved = (
        has_unresolved_scheduling(record) if unresolved is None else unresolved
    )
    if unresolved:
        return PendingDecision(
            kind="resume" if record.task is not None else "clarify",
            message=(
                message
                if record.task is not None
                else ARCHIVED_RECONCILIATION_MESSAGE
            ),
        )
    if record.task is not None and any(
        node.status == "failed" for node in record.task.nodes
    ):
        return PendingDecision(kind="retry", message=message)
    if record.task is not None and record.task.baseline.confirmed:
        return PendingDecision(kind="resume", message=message)
    return PendingDecision(kind="clarify", message=message)


def conservative_failure_transition(
    record: Conversation,
    message: str,
) -> tuple[Literal["failed", "interrupted"], PendingDecision]:
    unresolved = has_unresolved_scheduling(record)
    pending = conservative_pending_decision(
        record,
        message,
        unresolved=unresolved,
    )
    status: Literal["failed", "interrupted"] = (
        "interrupted"
        if unresolved or pending.kind == "resume"
        else "failed"
    )
    return status, pending


def recover_scheduling(record: Conversation) -> bool:
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    has_running = any(call.status == "running" for task in tasks for call in task.calls)
    if record.status not in ("queued", "planning", "running") and not has_running:
        return False
    has_queued_node = any(
        node.status == "queued" for task in tasks for node in task.nodes
    )
    has_unknown = has_unresolved_scheduling(record)
    for task in tasks:
        unresolved_nodes = {
            call.node_id
            for call in task.calls
            if call.status in ("running", "interrupted")
        }
        changed = False
        for call in task.calls:
            if call.status == "running":
                call.status = "interrupted"
                call.error = RESTART_MESSAGE
                call.finished_at = datetime.now(UTC).isoformat()
                changed = True
        for node in task.nodes:
            if node.status == "running" or node.id in unresolved_nodes:
                node.status = "interrupted"
                node.error = RESTART_MESSAGE
                changed = True
            elif node.status == "queued":
                node.status = "pending"
                changed = True
        if changed:
            task.stage = "interrupted"
    origin = record.queue_origin
    action = record.queued_action
    committed_action = record.queued_action_committed
    record.queued_action = None
    record.queued_action_committed = False
    record.queue_origin = None
    restored = False
    if (
        not has_unknown
        and not has_queued_node
        and not committed_action
        and action in ("confirm", "resume", "retry")
        and origin is not None
    ):
        record.status = origin.status
        record.pending_decision = origin.pending_decision
        record.error = origin.error
        restored = True
    elif not has_queued_node and record.status == "planning":
        restored = record.restore_turn_origin()
    elif not has_unknown and not has_queued_node and record.status == "queued":
        restored = record.restore_turn_origin()
    if not restored:
        recovery_message = (
            ARCHIVED_RECONCILIATION_MESSAGE
            if has_unknown and record.task is None
            else RESTART_MESSAGE
        )
        record.status = "interrupted"
        record.error = recovery_message
        record.pending_decision = conservative_pending_decision(
            record,
            recovery_message,
            unresolved=has_unknown,
        )
        record.turn_origin = None
        if record.task is not None:
            record.task.stage = "interrupted"
    else:
        recovery_message = RESTART_MESSAGE
    record.messages.append(
        ConversationMessage(
            role="assistant",
            content=recovery_message,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    return True
