from collections.abc import Callable
from typing import Literal

from ..models import Contract, Nonempty
from .agent_contracts import AgentScope
from .capabilities import revalidate_scope
from .contracts import Effect, ExecutionScope


class LoopDecision(Contract):
    action: Literal["continue", "stop", "clarify"]
    effect: Effect = "read_only"
    reason: Nonempty


def run_loop(
    *,
    decide: Callable[[], LoopDecision],
    execute: Callable[[], None],
    persist: Callable[[], None],
    scope: ExecutionScope | AgentScope,
    max_steps: int,
) -> LoopDecision:
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("The execution step budget must be a positive integer")
    scope = revalidate_scope(scope)
    steps = 0
    while True:
        decision = LoopDecision.model_validate(decide().model_dump())
        if decision.action != "continue":
            return decision
        if steps >= max_steps:
            raise ValueError("The confirmed execution step budget is exhausted")
        scope.authorize(decision.effect)
        execute()
        persist()
        steps += 1
