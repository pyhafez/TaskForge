"""Pure lifecycle and retry rules: no Django or queue dependencies."""

import random
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class State(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DEAD = "DEAD"


ALLOWED_TRANSITIONS = {
    State.PENDING: {State.QUEUED},
    State.QUEUED: {State.RUNNING},
    State.RUNNING: {State.SUCCEEDED, State.FAILED},
    State.FAILED: {State.RETRYING, State.DEAD},
    State.RETRYING: {State.QUEUED},
    State.SUCCEEDED: set(),
    State.DEAD: {State.QUEUED},
}


class InvalidTransition(ValueError):
    pass


def transition(source: State | str, target: State | str) -> State:
    source, target = State(source), State(target)
    if target not in ALLOWED_TRANSITIONS[source]:
        raise InvalidTransition(f"Cannot transition from {source} to {target}")
    return target


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    max_attempts: int = Field(default=3, ge=1, le=20)
    base_delay: float = Field(default=1, ge=0, le=3600)
    max_delay: float = Field(default=300, ge=0, le=86400)
    jitter: float = Field(default=0.2, ge=0, le=1)

    def can_retry(self, completed_attempts: int) -> bool:
        return completed_attempts < self.max_attempts

    def delay(self, completed_attempts: int, *, random_value: float | None = None) -> float:
        sample = random.random() if random_value is None else random_value
        if not 0 <= sample <= 1:
            raise ValueError("Random sample must be between zero and one")
        ceiling = min(
            self.max_delay, self.base_delay * 2 ** min(max(completed_attempts - 1, 0), 30)
        )
        return ceiling * (1 - self.jitter + self.jitter * sample)
