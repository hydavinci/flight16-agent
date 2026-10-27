from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import TypeAdapter

from .agent_contracts import BudgetState
from .contracts import Name, ResourceBudget


class BudgetExhausted(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class BudgetAccount:
    def __init__(
        self,
        budget: ResourceBudget,
        state: BudgetState,
        checkpoint: Callable[[BudgetState], None],
    ):
        self.budget = ResourceBudget.model_validate(budget.model_dump())
        self.state = BudgetState.model_validate(state)
        self._checkpoint = checkpoint
        if self.state.started_at is None:
            self._save(started_at=utc_now().isoformat())
        self.remaining_seconds()

    def _save(self, **changes: object) -> None:
        state = BudgetState.model_validate({**self.state.model_dump(), **changes})
        self._checkpoint(state)
        self.state = state

    def remaining_seconds(self) -> float:
        if self.state.started_at is None:
            raise ValueError("Execution budget has no persisted start time")
        started = datetime.fromisoformat(self.state.started_at)
        if started.tzinfo is None:
            raise ValueError("Execution budget requires an explicit time zone")
        elapsed = (utc_now() - started).total_seconds()
        if elapsed < 0:
            raise ValueError("Execution budget clock moved backwards")
        return max(0.0, self.budget.max_seconds - elapsed)

    def check_limits(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExhausted("The confirmed elapsed-time budget is exhausted")
        if self.state.no_progress >= self.budget.max_no_progress:
            raise BudgetExhausted("The confirmed no-progress budget is exhausted")

    def charge_call(self) -> None:
        self.check_limits()
        if self.state.calls >= self.budget.max_calls:
            raise BudgetExhausted("The confirmed call budget is exhausted")
        self._save(calls=self.state.calls + 1)

    def charge_round(self, *, key: str | None = None) -> None:
        if key is not None:
            key = TypeAdapter(Name).validate_python(key)
            if key in self.state.round_charges:
                return
        self.check_limits()
        if self.state.rounds >= self.budget.max_rounds:
            raise BudgetExhausted("The confirmed round budget is exhausted")
        self._save(
            rounds=self.state.rounds + 1,
            round_charges=[*self.state.round_charges, *([key] if key is not None else [])],
        )

    def note_progress(self, new_evidence: bool) -> None:
        if type(new_evidence) is not bool:
            raise ValueError("Execution progress must be an explicit boolean")
        self._save(no_progress=0 if new_evidence else self.state.no_progress + 1)
        self.check_limits()
