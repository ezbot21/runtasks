from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, cast
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from runtasks.config import DEFAULT_TIMEZONE
from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.handler_contracts import HANDLER_ACTION_MODES


SOURCE_TYPES = frozenset({"session", "document", "direct", "existing-task"})
ACTION_MODES = frozenset({"check", "notify", "approved-procedure"})
TASK_FIELDS = frozenset(
    {
        "name",
        "description",
        "source_type",
        "source_ref",
        "source_summary",
        "schedule",
        "timezone",
        "next_run_at",
        "action_mode",
        "handler",
        "policy",
        "enabled",
    }
)
REQUIRED_TASK_FIELDS = TASK_FIELDS - {"enabled", "timezone"}
MAX_POLICY_BYTES = 65_536
MAX_POLICY_DEPTH = 8
_IANA_TIMEZONES = frozenset(available_timezones())
_NON_IANA_TIMEZONE_NAMES = frozenset({"localtime", "posixrules"})
_NON_IANA_TIMEZONE_PREFIXES = ("posix/", "right/")
_FORBIDDEN_POLICY_KEY_PARTS = frozenset(
    {
        "argv",
        "authorization",
        "command",
        "commands",
        "credential",
        "credentials",
        "executable",
        "password",
        "script",
        "secret",
        "shell",
        "token",
    }
)
_FORBIDDEN_POLICY_KEY_PHRASES = frozenset({"api_key", "private_key"})
_POLICY_CAMEL_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)
_POLICY_KEY_PART = re.compile(r"[^a-z0-9]+")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(?:token|password|secret|api[_ -]?key)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"https?://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)


class TaskError(RuntimeError):
    """Raised when a task registry operation cannot be completed safely."""


class TaskValidationError(TaskError):
    """Raised when task input does not satisfy the public contract."""


class TaskNotFoundError(TaskError):
    """Raised when a requested task does not exist."""


class TaskConflictError(TaskError):
    """Raised when adding or updating would create a duplicate task."""

    def __init__(self, reason: str, existing_task_id: str) -> None:
        super().__init__(
            f"{reason} task already exists; update task {existing_task_id} instead"
        )
        self.reason = reason
        self.existing_task_id = existing_task_id


@dataclass(frozen=True)
class DailySchedule:
    time: str
    schedule_type: str = "daily"

    def as_dict(self) -> dict[str, object]:
        return {"time": self.time, "type": self.schedule_type}

    def human_description(self) -> str:
        return f"daily at {self.time}"


@dataclass(frozen=True)
class IntervalDaysSchedule:
    days: int
    time: str
    schedule_type: str = "interval-days"

    def as_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "time": self.time,
            "type": self.schedule_type,
        }

    def human_description(self) -> str:
        return f"every {self.days} days at {self.time}"


Schedule = DailySchedule | IntervalDaysSchedule


@dataclass(frozen=True)
class ScheduledOccurrence:
    scheduled_for: str
    next_run_at: str
    missed_occurrences_skipped: int


@dataclass(frozen=True)
class TaskInput:
    name: str
    description: str
    source_type: str
    source_ref: str | None
    source_summary: str
    schedule: Schedule
    timezone_name: str
    next_run_at: str
    action_mode: str
    handler: str
    policy: dict[str, object]
    enabled: bool

    @property
    def identity_key(self) -> str:
        identity = {"name": _normalize_identity_text(self.name)}
        return _fingerprint(identity)

    @property
    def source_identity_key(self) -> str | None:
        if self.source_ref is None:
            return None
        return _fingerprint(
            {
                "source_ref": _normalize_identity_text(self.source_ref),
                "source_type": self.source_type,
            }
        )

    @property
    def policy_key(self) -> str:
        policy_identity = {
            "action_mode": self.action_mode,
            "handler": self.handler,
            "policy": self.policy,
            "schedule": self.schedule.as_dict(),
            "timezone": self.timezone_name,
        }
        return _fingerprint(policy_identity)


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    description: str
    source_type: str
    source_ref: str | None
    source_summary: str
    schedule: Schedule
    timezone_name: str
    next_run_at: str
    action_mode: str
    handler: str
    policy: dict[str, object]
    enabled: bool
    created_at: str
    updated_at: str
    removed_at: str | None

    @property
    def next_run_local(self) -> str:
        due_at = datetime.fromisoformat(self.next_run_at.replace("Z", "+00:00"))
        local_due_at = due_at.astimezone(ZoneInfo(self.timezone_name))
        return (
            f"{local_due_at.isoformat(timespec='seconds')}"
            f"[{self.timezone_name}]"
        )

    @property
    def human_availability(self) -> str:
        if self.removed_at is not None:
            return "removed (unavailable for scheduled execution)"
        if self.enabled:
            return "enabled"
        return "disabled (unavailable for scheduled execution)"

    def as_dict(self) -> dict[str, object]:
        return {
            "action_mode": self.action_mode,
            "available_for_scheduled_execution": (
                self.enabled and self.removed_at is None
            ),
            "created_at": self.created_at,
            "description": self.description,
            "enabled": self.enabled,
            "handler": self.handler,
            "id": self.id,
            "name": self.name,
            "next_run_at": self.next_run_at,
            "next_run_local": self.next_run_local,
            "policy": self.policy,
            "removed_at": self.removed_at,
            "schedule": self.schedule.as_dict(),
            "source_ref": self.source_ref,
            "source_summary": self.source_summary,
            "source_type": self.source_type,
            "status": (
                "removed"
                if self.removed_at is not None
                else ("enabled" if self.enabled else "disabled")
            ),
            "timezone": self.timezone_name,
            "updated_at": self.updated_at,
        }


def next_scheduled_occurrence(
    task: Task,
    current_time: datetime,
) -> ScheduledOccurrence:
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise TaskValidationError("scheduler current time must include a UTC offset")
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
        next_date += timedelta(days=interval_days)
        next_local = datetime.combine(next_date, schedule_time, tzinfo=task_timezone)
        next_utc = next_local.astimezone(timezone.utc)
        round_trip = next_utc.astimezone(task_timezone)
        if round_trip.replace(tzinfo=None) != next_local.replace(tzinfo=None):
            missed_occurrences_skipped += 1
            continue
        if next_utc > current_time:
            return ScheduledOccurrence(
                scheduled_for=task.next_run_at,
                next_run_at=_canonical_utc_timestamp(next_utc),
                missed_occurrences_skipped=missed_occurrences_skipped,
            )
        missed_occurrences_skipped += 1


def parse_task_add_json(payload_json: str) -> TaskInput:
    values = _parse_json_object(payload_json)
    missing = sorted(REQUIRED_TASK_FIELDS - values.keys())
    if missing:
        raise TaskValidationError(
            "task input is missing required fields: " + ", ".join(missing)
        )
    return _task_input_from_values(values)


def parse_task_update_json(payload_json: str, current: Task) -> TaskInput:
    updates = _parse_json_object(payload_json)
    if not updates:
        raise TaskValidationError("task update must contain at least one field")
    unknown = sorted(updates.keys() - TASK_FIELDS)
    if unknown:
        raise TaskValidationError("task input contains unsupported fields")
    values: dict[str, Any] = {
        "action_mode": current.action_mode,
        "description": current.description,
        "enabled": current.enabled,
        "handler": current.handler,
        "name": current.name,
        "next_run_at": current.next_run_at,
        "policy": current.policy,
        "schedule": current.schedule.as_dict(),
        "source_ref": current.source_ref,
        "source_summary": current.source_summary,
        "source_type": current.source_type,
        "timezone": current.timezone_name,
    }
    values.update(updates)
    return _task_input_from_values(values)


def create_task(path: Path, task_input: TaskInput) -> Task:
    task_id = f"tsk_{uuid.uuid4().hex[:24]}"
    timestamp = _utc_now()
    try:
        with _task_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_duplicate(connection, task_input)
            connection.execute(
                """
                INSERT INTO tasks(
                    id, identity_key, source_identity_key, policy_key,
                    name, description, source_type, source_ref, source_summary,
                    schedule_type, schedule_json, timezone, next_run_at,
                    action_mode, handler, policy_json, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, *_task_database_values(task_input), timestamp, timestamp),
            )
            connection.commit()
            return get_task(path, task_id)
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("task could not be created") from error


def get_task(path: Path, task_id: str, *, include_removed: bool = False) -> Task:
    removed_filter = "" if include_removed else " AND removed_at IS NULL"
    try:
        with _task_connection(path) as connection:
            row = connection.execute(
                f"SELECT * FROM tasks WHERE id = ?{removed_filter}",
                (task_id,),
            ).fetchone()
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("task could not be inspected") from error
    if row is None:
        raise TaskNotFoundError("task does not exist")
    return _task_from_row(row)


def list_tasks(path: Path) -> list[Task]:
    try:
        with _task_connection(path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE removed_at IS NULL
                ORDER BY name COLLATE NOCASE, id
                """
            ).fetchall()
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("tasks could not be listed") from error
    return [_task_from_row(row) for row in rows]


def list_due_tasks(path: Path, current_time: str) -> list[Task]:
    try:
        with _task_connection(path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE enabled = 1
                  AND removed_at IS NULL
                  AND next_run_at <= ?
                ORDER BY next_run_at, id
                """,
                (current_time,),
            ).fetchall()
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("due Tasks could not be selected") from error
    return [_task_from_row(row) for row in rows]


def update_task(path: Path, task_id: str, task_input: TaskInput) -> Task:
    timestamp = _utc_now()
    try:
        with _task_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT id FROM tasks WHERE id = ? AND removed_at IS NULL",
                (task_id,),
            ).fetchone()
            if current_row is None:
                raise TaskNotFoundError("task does not exist")
            try:
                connection.execute(
                    """
                    UPDATE tasks SET
                        identity_key = ?, source_identity_key = ?, policy_key = ?,
                        name = ?, description = ?, source_type = ?, source_ref = ?,
                        source_summary = ?,
                        schedule_type = ?, schedule_json = ?, timezone = ?,
                        next_run_at = ?, action_mode = ?, handler = ?, policy_json = ?,
                        enabled = ?, updated_at = ?
                    WHERE id = ? AND removed_at IS NULL
                    """,
                    (*_task_database_values(task_input), timestamp, task_id),
                )
            except sqlite3.IntegrityError:
                _raise_duplicate(
                    connection,
                    task_input,
                    excluding_task_id=task_id,
                )
                raise
            connection.commit()
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("task could not be updated") from error
    return get_task(path, task_id)


def set_task_enabled(path: Path, task_id: str, enabled: bool) -> Task:
    current = get_task(path, task_id)
    if current.enabled == enabled:
        return current
    timestamp = _utc_now()
    try:
        with _task_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tasks SET enabled = ?, updated_at = ?
                WHERE id = ? AND removed_at IS NULL
                """,
                (int(enabled), timestamp, task_id),
            )
            if cursor.rowcount != 1:
                raise TaskNotFoundError("task does not exist")
            connection.commit()
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("task lifecycle could not be changed") from error
    return replace(current, enabled=enabled, updated_at=timestamp)


def remove_task(path: Path, task_id: str) -> None:
    try:
        with _task_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = _utc_now()
            cursor = connection.execute(
                """
                UPDATE tasks SET
                    identity_key = ?, source_identity_key = NULL,
                    policy_key = ?, enabled = 0, updated_at = ?, removed_at = ?
                WHERE id = ? AND removed_at IS NULL
                """,
                (
                    f"removed:{task_id}:identity",
                    f"removed:{task_id}:policy",
                    timestamp,
                    timestamp,
                    task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskNotFoundError("task does not exist")
            connection.commit()
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("task could not be removed") from error


def search_tasks(path: Path, query: str) -> list[Task]:
    normalized_query = query.strip()
    if not normalized_query:
        raise TaskValidationError("search query must not be empty")
    if len(normalized_query) > 500:
        raise TaskValidationError("search query must be at most 500 characters")
    fts_query = _literal_fts_query(normalized_query)
    try:
        with _task_connection(path) as connection:
            rows = connection.execute(
                """
                SELECT tasks.*
                FROM task_fts
                JOIN tasks ON tasks.id = task_fts.task_id
                WHERE task_fts MATCH ? AND tasks.removed_at IS NULL
                ORDER BY bm25(task_fts), tasks.name COLLATE NOCASE
                """,
                (fts_query,),
            ).fetchall()
    except sqlite3.OperationalError as error:
        raise TaskError("task search failed") from error
    except TaskError:
        raise
    except sqlite3.Error as error:
        raise TaskError("task search failed") from error
    return [_task_from_row(row) for row in rows]


def _task_input_from_values(values: Mapping[str, Any]) -> TaskInput:
    unknown = sorted(values.keys() - TASK_FIELDS)
    if unknown:
        raise TaskValidationError("task input contains unsupported fields")

    name = _required_string(values, "name", maximum=200)
    description = _required_string(values, "description", maximum=4_000)
    source_type = _required_string(values, "source_type", maximum=50)
    if source_type not in SOURCE_TYPES:
        raise TaskValidationError(
            "source_type must be session, document, direct, or existing-task"
        )
    source_ref = _optional_string(values, "source_ref", maximum=2_000)
    source_summary = _required_string(values, "source_summary", maximum=8_000)
    schedule = _validate_schedule(values.get("schedule"))
    timezone_name, task_timezone = _validate_timezone(
        values.get("timezone", DEFAULT_TIMEZONE)
    )
    next_run_at, next_run_datetime = _validate_next_run_at(values.get("next_run_at"))
    local_next_run = next_run_datetime.astimezone(task_timezone)
    if (
        local_next_run.strftime("%H:%M") != schedule.time
        or local_next_run.second != 0
        or local_next_run.microsecond != 0
    ):
        raise TaskValidationError(
            "next_run_at must occur exactly at the schedule time in the task timezone"
        )
    action_mode = _required_string(values, "action_mode", maximum=50)
    if action_mode not in ACTION_MODES:
        raise TaskValidationError(
            "action_mode must be check, notify, or approved-procedure"
        )
    handler = _required_string(values, "handler", maximum=100)
    supported_modes = HANDLER_ACTION_MODES.get(handler)
    if supported_modes is None:
        raise TaskValidationError("handler is not registered")
    if action_mode not in supported_modes:
        raise TaskValidationError("handler does not support the selected action_mode")
    policy = _validate_policy(values.get("policy"))
    enabled_value = values.get("enabled", True)
    if not isinstance(enabled_value, bool):
        raise TaskValidationError("enabled must be a boolean")

    return TaskInput(
        name=name,
        description=description,
        source_type=source_type,
        source_ref=source_ref,
        source_summary=source_summary,
        schedule=schedule,
        timezone_name=timezone_name,
        next_run_at=next_run_at,
        action_mode=action_mode,
        handler=handler,
        policy=policy,
        enabled=enabled_value,
    )


def _parse_json_object(payload_json: str) -> dict[str, Any]:
    try:
        parsed: object = json.loads(
            payload_json,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except DuplicateJsonKeyError as error:
        raise TaskValidationError(
            "task input must not contain duplicate object keys"
        ) from error
    except (json.JSONDecodeError, UnicodeError) as error:
        raise TaskValidationError("task input must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise TaskValidationError("task input must be a JSON object")
    return cast(dict[str, Any], parsed)


class DuplicateJsonKeyError(ValueError):
    """Raised internally when a JSON object repeats a key."""


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError
        result[key] = value
    return result


def _required_string(
    values: Mapping[str, Any], field: str, *, maximum: int
) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise TaskValidationError(f"{field} is too long")
    if "\x00" in value:
        raise TaskValidationError(f"{field} contains unsupported control characters")
    return value.strip()


def _optional_string(
    values: Mapping[str, Any], field: str, *, maximum: int
) -> str | None:
    value = values.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{field} must be null or a non-empty string")
    if len(value) > maximum:
        raise TaskValidationError(f"{field} is too long")
    if "\x00" in value:
        raise TaskValidationError(f"{field} contains unsupported control characters")
    return value.strip()


def _validate_schedule(value: object) -> Schedule:
    if not isinstance(value, dict):
        raise TaskValidationError("schedule must be a JSON object")
    schedule = cast(dict[str, Any], value)
    schedule_type = schedule.get("type")
    if schedule_type == "daily":
        if set(schedule) != {"type", "time"}:
            raise TaskValidationError("daily schedule supports only type and time")
        return DailySchedule(time=_validate_time(schedule.get("time")))
    if schedule_type == "interval-days":
        if set(schedule) != {"type", "days", "time"}:
            raise TaskValidationError(
                "interval-days schedule requires type, days, and time"
            )
        days = schedule.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3_650:
            raise TaskValidationError(
                "interval-days schedule days must be an integer from 1 to 3650"
            )
        return IntervalDaysSchedule(
            days=days,
            time=_validate_time(schedule.get("time")),
        )
    raise TaskValidationError("schedule type must be daily or interval-days")


def _validate_time(value: object) -> str:
    if not isinstance(value, str):
        raise TaskValidationError("schedule time must use 24-hour HH:MM format")
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as error:
        raise TaskValidationError(
            "schedule time must use 24-hour HH:MM format"
        ) from error
    if parsed.strftime("%H:%M") != value:
        raise TaskValidationError("schedule time must use 24-hour HH:MM format")
    return value


def _validate_timezone(value: object) -> tuple[str, ZoneInfo]:
    if (
        not isinstance(value, str)
        or value not in _IANA_TIMEZONES
        or value in _NON_IANA_TIMEZONE_NAMES
        or value.startswith(_NON_IANA_TIMEZONE_PREFIXES)
    ):
        raise TaskValidationError("timezone must name an installed IANA timezone")
    try:
        task_timezone = ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise TaskValidationError(
            "timezone must name an installed IANA timezone"
        ) from error
    return str(task_timezone), task_timezone


def _validate_next_run_at(value: object) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value:
        raise TaskValidationError("next_run_at must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TaskValidationError("next_run_at must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TaskValidationError("next_run_at must include a UTC offset")
    utc_value = parsed.astimezone(timezone.utc)
    canonical = utc_value.isoformat(timespec="seconds").replace("+00:00", "Z")
    return canonical, utc_value


def _validate_policy(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TaskValidationError("policy must be a JSON object")
    policy = cast(dict[str, object], value)
    _validate_policy_value(policy, depth=0)
    if len(_canonical_json(policy).encode("utf-8")) > MAX_POLICY_BYTES:
        raise TaskValidationError("policy exceeds the 65536-byte safety limit")
    return policy


def _validate_policy_value(value: object, *, depth: int) -> None:
    if depth > MAX_POLICY_DEPTH:
        raise TaskValidationError("policy nesting exceeds the safety limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TaskValidationError("policy numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > 10_000:
            raise TaskValidationError("policy text value is too long")
        if "\x00" in value:
            raise TaskValidationError("policy contains unsupported control characters")
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            raise TaskValidationError("policy contains secret-like values")
        return
    if isinstance(value, list):
        if len(value) > 1_000:
            raise TaskValidationError("policy list exceeds the safety limit")
        for item in value:
            _validate_policy_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 500:
            raise TaskValidationError("policy object exceeds the safety limit")
        for raw_key, item in cast(dict[object, object], value).items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise TaskValidationError("policy keys must be non-empty strings")
            separated_key = _POLICY_CAMEL_BOUNDARY.sub("_", raw_key)
            normalized_key = _POLICY_KEY_PART.sub(
                "_", separated_key.casefold()
            ).strip("_")
            key_parts = frozenset(normalized_key.split("_"))
            if (
                key_parts & _FORBIDDEN_POLICY_KEY_PARTS
                or any(
                    f"_{phrase}_" in f"_{normalized_key}_"
                    for phrase in _FORBIDDEN_POLICY_KEY_PHRASES
                )
            ):
                raise TaskValidationError(
                    "policy contains executable or secret-bearing fields"
                )
            _validate_policy_value(item, depth=depth + 1)
        return
    raise TaskValidationError("policy contains an unsupported JSON value")


def _task_database_values(task_input: TaskInput) -> tuple[object, ...]:
    return (
        task_input.identity_key,
        task_input.source_identity_key,
        task_input.policy_key,
        task_input.name,
        task_input.description,
        task_input.source_type,
        task_input.source_ref,
        task_input.source_summary,
        task_input.schedule.schedule_type,
        _canonical_json(task_input.schedule.as_dict()),
        task_input.timezone_name,
        task_input.next_run_at,
        task_input.action_mode,
        task_input.handler,
        _canonical_json(task_input.policy),
        int(task_input.enabled),
    )


def _raise_duplicate(
    connection: sqlite3.Connection,
    task_input: TaskInput,
    *,
    excluding_task_id: str | None = None,
) -> None:
    conditions = ["identity_key = ?", "policy_key = ?"]
    parameters = [task_input.identity_key, task_input.policy_key]
    if task_input.source_identity_key is not None:
        conditions.append("source_identity_key = ?")
        parameters.append(task_input.source_identity_key)
    exclusion_sql = ""
    if excluding_task_id is not None:
        exclusion_sql = " AND id != ?"
        parameters.append(excluding_task_id)
    row = connection.execute(
        f"""
        SELECT id, identity_key, source_identity_key
        FROM tasks
        WHERE removed_at IS NULL
          AND ({' OR '.join(conditions)}){exclusion_sql}
        LIMIT 1
        """,
        tuple(parameters),
    ).fetchone()
    if row is None:
        return
    identity_equivalent = row["identity_key"] == task_input.identity_key
    if task_input.source_identity_key is not None:
        identity_equivalent = identity_equivalent or (
            row["source_identity_key"] == task_input.source_identity_key
        )
    reason = "identity-equivalent" if identity_equivalent else "policy-equivalent"
    raise TaskConflictError(reason, str(row["id"]))


def _task_from_row(row: sqlite3.Row) -> Task:
    schedule = _validate_schedule(json.loads(str(row["schedule_json"])))
    policy = cast(dict[str, object], json.loads(str(row["policy_json"])))
    return Task(
        id=str(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        source_type=str(row["source_type"]),
        source_ref=None if row["source_ref"] is None else str(row["source_ref"]),
        source_summary=str(row["source_summary"]),
        schedule=schedule,
        timezone_name=str(row["timezone"]),
        next_run_at=str(row["next_run_at"]),
        action_mode=str(row["action_mode"]),
        handler=str(row["handler"]),
        policy=policy,
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        removed_at=(
            None if row["removed_at"] is None else str(row["removed_at"])
        ),
    )


@contextmanager
def _task_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise TaskError("runtime is not initialized; run 'runtasks init' first")
    with database_connection(path, enable_wal=False) as connection:
        connection.row_factory = sqlite3.Row
        try:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error as error:
            raise TaskError(
                "database schema is not current; run 'runtasks init'"
            ) from error
        version = 0 if version_row is None else int(version_row[0])
        if version != LATEST_SCHEMA_VERSION:
            raise TaskError("database schema is not current; run 'runtasks init'")
        yield connection


def _literal_fts_query(query: str) -> str:
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        raise TaskValidationError("search query must contain searchable text")
    return " AND ".join(f'"{term}"' for term in terms)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_identity_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _canonical_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
