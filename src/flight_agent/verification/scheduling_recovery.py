from datetime import UTC, datetime

from .contracts import Conversation, ConversationMessage, PendingDecision

RESTART_MESSAGE = (
    "Service restarted before this request completed. No queued request was replayed; "
    "review the saved state and explicitly resend, confirm, or resume."
)


def recover_scheduling(record: Conversation) -> bool:
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    has_running = any(call.status == "running" for task in tasks for call in task.calls)
    if record.status not in ("queued", "planning", "running") and not has_running:
        return False
    has_queued_node = any(
        node.status == "queued" for task in tasks for node in task.nodes
    )
    has_unknown = any(
        call.status in ("running", "interrupted")
        for task in tasks
        for call in task.calls
    )
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
        record.status = "interrupted"
        record.error = RESTART_MESSAGE
        confirmed = record.task is not None and record.task.baseline.confirmed
        failed = record.task is not None and any(
            node.status == "failed" for node in record.task.nodes
        )
        record.pending_decision = PendingDecision(
            kind=(
                "resume"
                if has_unknown
                else "retry"
                if committed_action and failed
                else "resume"
                if confirmed
                else "clarify"
            ),
            message=RESTART_MESSAGE,
        )
        record.turn_origin = None
        if record.task is not None:
            record.task.stage = "interrupted"
    record.messages.append(
        ConversationMessage(
            role="assistant",
            content=RESTART_MESSAGE,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    return True
