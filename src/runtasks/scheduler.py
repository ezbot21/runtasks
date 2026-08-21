from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from runtasks.adapters import ExternalAdapter
from runtasks.handlers import HandlerRegistry
from runtasks.redaction import Redactor
from runtasks.runs import (
    Run,
    ScheduledClaim,
    claim_scheduled_run,
    execute_scheduled_run,
)
from runtasks.tasks import list_due_tasks


class SchedulerValidationError(ValueError):
    """Raised when the scheduler clock is not deterministic and timezone-aware."""


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the scheduler's current timezone-aware time."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FixedClock:
    current_time: datetime

    def now(self) -> datetime:
        return self.current_time


@dataclass(frozen=True)
class SchedulerResult:
    current_time: str
    runs: tuple[Run, ...]

    @property
    def status(self) -> str:
        return "executed" if self.runs else "no-due-work"


def parse_scheduler_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SchedulerValidationError(
            "scheduler time must be an offset-aware RFC 3339 timestamp"
        ) from error
    return _require_aware(parsed)


def run_due_tasks(
    path: Path,
    clock: Clock,
    external_adapter: ExternalAdapter,
    handler_registry: HandlerRegistry,
    redactor: Redactor,
) -> SchedulerResult:
    current_datetime = _require_aware(clock.now()).astimezone(timezone.utc)
    current_time = _canonical_timestamp(current_datetime)
    claimed: list[ScheduledClaim] = []

    for due_task in list_due_tasks(path, current_time):
        claim = claim_scheduled_run(
            path,
            due_task.id,
            claimed_at=current_time,
        )
        if claim is not None:
            claimed.append(claim)

    runs = tuple(
        execute_scheduled_run(
            path,
            claim.run_id,
            claim.task,
            external_adapter,
            handler_registry,
            redactor,
            lambda: _clock_timestamp(clock),
        )
        for claim in claimed
    )
    return SchedulerResult(current_time=current_time, runs=runs)


def _clock_timestamp(clock: Clock) -> str:
    return _canonical_timestamp(_require_aware(clock.now()))


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchedulerValidationError("scheduler clock must return a timezone-aware time")
    return value


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
