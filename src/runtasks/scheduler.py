from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from runtasks.adapters import ExternalAdapter
from runtasks.handlers import HandlerRegistry
from runtasks.redaction import Redactor
from runtasks.runs import Run, claim_scheduled_run, execute_scheduled_run
from runtasks.tasks import IntervalDaysSchedule, Task, list_due_tasks


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
    claimed: list[tuple[str, Task]] = []

    for task in list_due_tasks(path, current_time):
        next_run_at, missed_occurrences_skipped = _next_run_after(
            task,
            current_datetime,
        )
        run_id = claim_scheduled_run(
            path,
            task,
            claimed_at=current_time,
            next_run_at=next_run_at,
            missed_occurrences_skipped=missed_occurrences_skipped,
        )
        if run_id is not None:
            claimed.append((run_id, task))

    runs = tuple(
        execute_scheduled_run(
            path,
            run_id,
            task,
            external_adapter,
            handler_registry,
            redactor,
        )
        for run_id, task in claimed
    )
    return SchedulerResult(current_time=current_time, runs=runs)


def _next_run_after(task: Task, current_time: datetime) -> tuple[str, int]:
    task_timezone = ZoneInfo(task.timezone_name)
    scheduled_at = datetime.fromisoformat(
        task.next_run_at.replace("Z", "+00:00")
    ).astimezone(task_timezone)
    interval_days = (
        task.schedule.days
        if isinstance(task.schedule, IntervalDaysSchedule)
        else 1
    )
    schedule_hour, schedule_minute = (
        int(part) for part in task.schedule.time.split(":")
    )
    schedule_time = time(hour=schedule_hour, minute=schedule_minute)
    next_date = scheduled_at.date()
    missed_occurrences_skipped = 0

    while True:
        next_date = _add_days(next_date, interval_days)
        next_local = datetime.combine(next_date, schedule_time, tzinfo=task_timezone)
        next_utc = next_local.astimezone(timezone.utc)
        if next_utc > current_time:
            return _canonical_timestamp(next_utc), missed_occurrences_skipped
        missed_occurrences_skipped += 1


def _add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchedulerValidationError("scheduler clock must return a timezone-aware time")
    return value


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
